"""Gearsmith: a small self-hosted gear tracker for guitarists."""

from __future__ import annotations

import hashlib
import hmac
from html import escape as html_escape
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("GEARSMITH_DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "gearsmith.db"
PHOTOS_DIR = DATA_DIR / "photos"
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

PHOTO_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
}
PHOTO_MAX_BYTES = 10 * 1024 * 1024
STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:jpg|png|webp|gif|heic)$")
UPLOAD_CHUNK = 1024 * 1024

COOKIE = "gearsmith_session"
SESSION_DAYS = 30
PBKDF2_ITERATIONS = 260_000
LOGIN_LIMIT = 5
LOGIN_WINDOW_SECONDS = 15 * 60
API_LIMIT = 100
API_WINDOW_SECONDS = 60
API_FAIL_LIMIT = 5
API_FAIL_WINDOW_SECONDS = 15 * 60

APP_VERSION = "0.4.0"

GEAR_TYPES = ("guitar", "amp", "pedal", "pick", "strings")
GEAR_TYPE_LABELS = {"guitar": "Guitars", "amp": "Amps", "pedal": "Pedals", "pick": "Picks", "strings": "Strings"}
GEAR_TYPE_SINGULAR = {"guitar": "Guitar", "amp": "Amp", "pedal": "Pedal", "pick": "Pick", "strings": "Strings"}
# Gear types whose make is a brand name on the card and the share page.
BRAND_TYPES = ("pick", "strings")
STRING_TYPES = ("electric", "acoustic", "classical", "bass")
GEAR_STATUS = ("", "home", "luthier", "lent")
GEAR_STATUS_LABELS = {"": "Unspecified", "home": "Home", "luthier": "At the luthier", "lent": "Lent out"}
# Lifecycle is separate from status: status says where owned gear is, lifecycle says whether
# you own it yet (want), own it, or used to (sold, kept as history).
LIFECYCLES = ("owned", "want", "sold")
LIFECYCLE_LABELS = {"owned": "Owned", "want": "Want", "sold": "Sold"}
# Gear types that can carry a list of named controls (knobs and switches) for song settings.
CONTROL_TYPES = ("guitar", "amp", "pedal")
CONTROL_KINDS = ("knob", "switch")
MAX_CONTROLS = 40

# Per-type spec sheet fields: (key, label, numeric?). Values live in the gear.specs JSON column.
SPEC_FIELDS: dict[str, list[tuple[str, str, bool]]] = {
    "guitar": [
        ("finish", "Finish", False),
        ("pickups", "Pickups", False),
        ("nut", "Nut", False),
        ("scale", "Scale length", False),
        ("tuning", "Tuning", False),
        ("string_gauge", "String gauge", False),
        ("mods", "Mods", False),
    ],
    "amp": [
        ("wattage", "Wattage", False),
        ("speaker", "Speaker", False),
        ("tubes", "Tubes", False),
    ],
    "pedal": [
        ("voltage", "Voltage", False),
        ("ma_draw", "Current draw (mA)", True),
        ("polarity", "Polarity", False),
        ("bypass", "Bypass", False),
    ],
    "pick": [
        ("thickness", "Thickness", False),
        ("material", "Material", False),
        ("quantity", "Quantity", True),
    ],
    "strings": [
        ("gauge", "Gauge", False),
        ("string_type", "String type", False),
        ("material", "Material", False),
        ("strings_per_set", "Strings per set", True),
        ("sets_per_pack", "Sets per pack", True),
    ],
}
# Spec fields that only take one of a few values.
SPEC_CHOICES: dict[str, dict[str, tuple[str, ...]]] = {"strings": {"string_type": STRING_TYPES}}

DEFAULT_RESTRING_INTERVAL = 90
# The strings chip turns yellow at this fraction of the interval, red past it.
STRING_WARN_FRACTION = 0.75

_login_failures: dict[str, deque[float]] = defaultdict(deque)
_login_lock = threading.Lock()
_api_calls: dict[str, deque[float]] = defaultdict(deque)
_api_failures: dict[str, deque[float]] = defaultdict(deque)
_api_lock = threading.Lock()

app = FastAPI(title="Gearsmith", version=APP_VERSION, docs_url=None, openapi_url=None)


# ---------------------------------------------------------------- helpers


def today() -> date:
    """Local date for the container's TZ, so 'due today' matches the wall clock."""
    return datetime.now().astimezone().date()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def check_date(value: str | None) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    value = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or iso_date(value) is None:
        raise ValueError("Dates must be YYYY-MM-DD")
    return value


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def set_setting(c, key: str, value: str) -> None:
    c.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_setting(c, key: str, default: str = "") -> str:
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# ---------------------------------------------------------------- security


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # the built-in tuner uses the mic; it stays on-device (no capture leaves the page)
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(self)"
    if request.url.path == "/api/docs":
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'; "
            "img-src 'self' data: https://fastapi.tiangolo.com; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net"
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; object-src 'none'; img-src 'self' data: blob:; "
            "script-src 'self'; style-src 'self' 'unsafe-inline'"
        )
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def client_ip(request: Request) -> str:
    # Uvicorn swaps in the real client address only when the proxy is trusted
    # (FORWARDED_ALLOW_IPS), so this is safe to rate limit on.
    return request.client.host if request.client else "unknown"


def _window(hits: deque[float], limit: int, window: int, count: bool) -> int:
    now = time.monotonic()
    while hits and now - hits[0] >= window:
        hits.popleft()
    if len(hits) >= limit:
        return max(1, int(window - (now - hits[0])) + 1)
    if count:
        hits.append(now)
    return 0


def login_retry_after(ip: str) -> int:
    with _login_lock:
        return _window(_login_failures[ip], LOGIN_LIMIT, LOGIN_WINDOW_SECONDS, False)


def record_login_failure(ip: str) -> None:
    with _login_lock:
        _login_failures[ip].append(time.monotonic())


def clear_login_failures(ip: str) -> None:
    with _login_lock:
        _login_failures.pop(ip, None)


# ---------------------------------------------------------------- database


def init_db() -> None:
    fresh = not DB_PATH.exists()
    with db() as c:
        c.executescript(
            """
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY,
          username TEXT NOT NULL UNIQUE COLLATE NOCASE,
          password_hash TEXT NOT NULL,
          salt TEXT NOT NULL,
          is_admin INTEGER NOT NULL DEFAULT 0,
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_tokens (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          prefix TEXT NOT NULL,
          created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL,
          last_used_at TEXT,
          revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS gear (
          id INTEGER PRIMARY KEY,
          type TEXT NOT NULL CHECK (type IN ('guitar','amp','pedal','pick','strings')),
          name TEXT NOT NULL,
          make TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          year INTEGER,
          serial TEXT NOT NULL DEFAULT '',
          specs TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT '',
          purchase_date TEXT,
          purchase_price REAL,
          notes TEXT NOT NULL DEFAULT '',
          restring_interval_days INTEGER,
          favorite INTEGER NOT NULL DEFAULT 0,
          lifecycle TEXT NOT NULL DEFAULT 'owned',
          want_price REAL,
          sold_date TEXT,
          sold_price REAL,
          sets_on_hand INTEGER,
          manual_url TEXT NOT NULL DEFAULT '',
          strings_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gear_photos (
          id INTEGER PRIMARY KEY,
          gear_id INTEGER NOT NULL REFERENCES gear(id) ON DELETE CASCADE,
          filename TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS restrings (
          id INTEGER PRIMARY KEY,
          gear_id INTEGER NOT NULL REFERENCES gear(id) ON DELETE CASCADE,
          brand TEXT NOT NULL DEFAULT '',
          gauge TEXT NOT NULL DEFAULT '',
          date TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          via TEXT NOT NULL DEFAULT '',
          strings_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sets (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL UNIQUE COLLATE NOCASE,
          notes TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS set_items (
          set_id INTEGER NOT NULL REFERENCES sets(id) ON DELETE CASCADE,
          gear_id INTEGER NOT NULL REFERENCES gear(id) ON DELETE CASCADE,
          sort INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (set_id, gear_id)
        );
        CREATE TABLE IF NOT EXISTS shares (
          id INTEGER PRIMARY KEY,
          token TEXT NOT NULL UNIQUE,
          gear_id INTEGER UNIQUE REFERENCES gear(id) ON DELETE CASCADE,
          set_id INTEGER UNIQUE REFERENCES sets(id) ON DELETE CASCADE,
          expires_at TEXT,
          created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          CHECK ((gear_id IS NULL) != (set_id IS NULL))
        );
        CREATE TABLE IF NOT EXISTS songs (
          id INTEGER PRIMARY KEY,
          title TEXT NOT NULL,
          artist TEXT NOT NULL DEFAULT '',
          tuning TEXT NOT NULL DEFAULT '',
          capo INTEGER,
          song_key TEXT NOT NULL DEFAULT '',
          bpm INTEGER,
          guitar_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          guitar_name TEXT NOT NULL DEFAULT '',
          amp_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          amp_name TEXT NOT NULL DEFAULT '',
          set_id INTEGER REFERENCES sets(id) ON DELETE SET NULL,
          set_name TEXT NOT NULL DEFAULT '',
          notes TEXT NOT NULL DEFAULT '',
          created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS song_gear_settings (
          id INTEGER PRIMARY KEY,
          song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
          gear_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          gear_name TEXT NOT NULL DEFAULT '',
          position INTEGER NOT NULL DEFAULT 0,
          engaged TEXT NOT NULL DEFAULT 'on',
          knobs TEXT NOT NULL DEFAULT '[]',
          note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS song_device_patches (
          id INTEGER PRIMARY KEY,
          song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
          gear_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          gear_name TEXT NOT NULL DEFAULT '',
          position INTEGER NOT NULL DEFAULT 0,
          patch_ref TEXT NOT NULL DEFAULT '',
          patch_name TEXT NOT NULL DEFAULT '',
          scenes TEXT NOT NULL DEFAULT '[]',
          midi TEXT,
          note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS song_effect_blocks (
          id INTEGER PRIMARY KEY,
          patch_id INTEGER NOT NULL REFERENCES song_device_patches(id) ON DELETE CASCADE,
          position INTEGER NOT NULL DEFAULT 0,
          slot TEXT NOT NULL DEFAULT '',
          block_type TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 1,
          params TEXT NOT NULL DEFAULT '[]',
          scene_overrides TEXT
        );
        CREATE TABLE IF NOT EXISTS song_photos (
          id INTEGER PRIMARY KEY,
          song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
          filename TEXT NOT NULL,
          sort INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS presets (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          artist TEXT NOT NULL DEFAULT '',
          amp_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          amp_name TEXT NOT NULL DEFAULT '',
          notes TEXT NOT NULL DEFAULT '',
          created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS preset_gear_settings (
          id INTEGER PRIMARY KEY,
          preset_id INTEGER NOT NULL REFERENCES presets(id) ON DELETE CASCADE,
          gear_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          gear_name TEXT NOT NULL DEFAULT '',
          position INTEGER NOT NULL DEFAULT 0,
          engaged TEXT NOT NULL DEFAULT 'on',
          knobs TEXT NOT NULL DEFAULT '[]',
          note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS preset_device_patches (
          id INTEGER PRIMARY KEY,
          preset_id INTEGER NOT NULL REFERENCES presets(id) ON DELETE CASCADE,
          gear_id INTEGER REFERENCES gear(id) ON DELETE SET NULL,
          gear_name TEXT NOT NULL DEFAULT '',
          position INTEGER NOT NULL DEFAULT 0,
          patch_ref TEXT NOT NULL DEFAULT '',
          patch_name TEXT NOT NULL DEFAULT '',
          scenes TEXT NOT NULL DEFAULT '[]',
          midi TEXT,
          note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS preset_effect_blocks (
          id INTEGER PRIMARY KEY,
          patch_id INTEGER NOT NULL REFERENCES preset_device_patches(id) ON DELETE CASCADE,
          position INTEGER NOT NULL DEFAULT 0,
          slot TEXT NOT NULL DEFAULT '',
          block_type TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 1,
          params TEXT NOT NULL DEFAULT '[]',
          scene_overrides TEXT
        );
        CREATE TABLE IF NOT EXISTS maintenance (
          id INTEGER PRIMARY KEY,
          gear_id INTEGER NOT NULL REFERENCES gear(id) ON DELETE CASCADE,
          date TEXT NOT NULL,
          category TEXT NOT NULL DEFAULT 'other',
          note TEXT NOT NULL DEFAULT '',
          user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS song_presets (
          id INTEGER PRIMARY KEY,
          song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
          preset_id INTEGER NOT NULL REFERENCES presets(id) ON DELETE CASCADE,
          position INTEGER NOT NULL DEFAULT 0,
          label TEXT NOT NULL DEFAULT '',
          note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_preset_settings ON preset_gear_settings(preset_id, position);
        CREATE INDEX IF NOT EXISTS idx_preset_patches ON preset_device_patches(preset_id, position);
        CREATE INDEX IF NOT EXISTS idx_preset_blocks ON preset_effect_blocks(patch_id, position);
        CREATE INDEX IF NOT EXISTS idx_song_presets ON song_presets(song_id, position);
        CREATE INDEX IF NOT EXISTS idx_preset_songs ON song_presets(preset_id);
        CREATE INDEX IF NOT EXISTS idx_gear_type ON gear(type);
        CREATE INDEX IF NOT EXISTS idx_song_settings ON song_gear_settings(song_id, position);
        CREATE INDEX IF NOT EXISTS idx_song_patches ON song_device_patches(song_id, position);
        CREATE INDEX IF NOT EXISTS idx_song_blocks ON song_effect_blocks(patch_id, position);
        CREATE INDEX IF NOT EXISTS idx_song_photos ON song_photos(song_id);
        CREATE INDEX IF NOT EXISTS idx_restrings_gear ON restrings(gear_id, date);
        CREATE INDEX IF NOT EXISTS idx_photos_gear ON gear_photos(gear_id);
        CREATE INDEX IF NOT EXISTS idx_maintenance_gear ON maintenance(gear_id, date);
        """
        )
        migrate(c)
        if fresh:
            seed_example(c)


def migrate(c) -> None:
    # Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an existing
    # database alone, so older installs get them here.
    gear_cols = {r["name"] for r in c.execute("PRAGMA table_info(gear)")}
    if "favorite" not in gear_cols:
        c.execute("ALTER TABLE gear ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
    # Want/owned/sold lifecycle. Everything that existed before is gear you own.
    if "lifecycle" not in gear_cols:
        c.execute("ALTER TABLE gear ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'owned'")
    if "want_price" not in gear_cols:
        c.execute("ALTER TABLE gear ADD COLUMN want_price REAL")
    if "sold_date" not in gear_cols:
        c.execute("ALTER TABLE gear ADD COLUMN sold_date TEXT")
    if "sold_price" not in gear_cols:
        c.execute("ALTER TABLE gear ADD COLUMN sold_price REAL")
    # 0.3.1: strings became a gear type, and guitars and restrings can point at a strings item.
    widen_gear_types(c)
    if "strings_id" not in gear_cols:
        c.execute("ALTER TABLE gear ADD COLUMN strings_id INTEGER REFERENCES gear(id) ON DELETE SET NULL")
    # 0.4.0: strings can track sets on hand, and every item can carry a manual link.
    if "sets_on_hand" not in gear_cols:
        c.execute("ALTER TABLE gear ADD COLUMN sets_on_hand INTEGER")
    if "manual_url" not in gear_cols:
        c.execute("ALTER TABLE gear ADD COLUMN manual_url TEXT NOT NULL DEFAULT ''")
    restring_cols = {r["name"] for r in c.execute("PRAGMA table_info(restrings)")}
    if "strings_id" not in restring_cols:
        c.execute("ALTER TABLE restrings ADD COLUMN strings_id INTEGER REFERENCES gear(id) ON DELETE SET NULL")
    c.execute("CREATE INDEX IF NOT EXISTS idx_gear_strings ON gear(strings_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_restrings_strings ON restrings(strings_id)")


GEAR_TYPE_CHECK = re.compile(r"CHECK\s*\(\s*type\s+IN\s*\([^)]*\)\s*\)", re.IGNORECASE)


def widen_gear_types(c) -> None:
    """Let an older database store the newer gear types.

    Databases made before 0.3.1 have a CHECK on gear.type that only allows guitar, amp, pedal
    and pick. SQLite can't change a CHECK in place, so the gear table is rebuilt once: every
    row is copied with the same id into a new table with the wider CHECK, and the new table
    takes the old one's name. A backup copy of the whole database is written first. Nothing
    else changes, and the rows that point at gear (photos, restrings, sets, songs) keep
    pointing at the same ids.
    """
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='gear'").fetchone()
    if not row or not GEAR_TYPE_CHECK.search(row[0]):
        return
    allowed = ",".join(f"'{t}'" for t in GEAR_TYPES)
    new_sql = GEAR_TYPE_CHECK.sub(f"CHECK (type IN ({allowed}))", row[0])
    if new_sql == row[0]:
        return
    new_sql = re.sub(r'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`\[]?gear["`\]]?',
                     "CREATE TABLE gear_new", new_sql, count=1, flags=re.IGNORECASE)
    c.commit()
    backup_database(c, "before-0.3.1")
    cols = ", ".join(f'"{r["name"]}"' for r in c.execute("PRAGMA table_info(gear)"))
    level = c.isolation_level
    c.isolation_level = None  # manage the transaction by hand
    c.execute("PRAGMA foreign_keys=OFF")
    try:
        c.execute("BEGIN IMMEDIATE")
        try:
            problems_before = len(c.execute("PRAGMA foreign_key_check").fetchall())
            count = c.execute("SELECT COUNT(*) FROM gear").fetchone()[0]
            c.execute("DROP TABLE IF EXISTS gear_new")
            c.execute(new_sql)
            c.execute(f"INSERT INTO gear_new ({cols}) SELECT {cols} FROM gear")
            if c.execute("SELECT COUNT(*) FROM gear_new").fetchone()[0] != count:
                raise RuntimeError("gear copy lost rows")
            c.execute("DROP TABLE gear")
            c.execute("ALTER TABLE gear_new RENAME TO gear")
            c.execute("CREATE INDEX IF NOT EXISTS idx_gear_type ON gear(type)")
            if len(c.execute("PRAGMA foreign_key_check").fetchall()) > problems_before:
                raise RuntimeError("gear rebuild broke a reference")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    finally:
        c.execute("PRAGMA foreign_keys=ON")
        c.isolation_level = level


def backup_database(c, label: str) -> Path:
    """Write a full copy of the database next to it, e.g. gearsmith-backup-before-0.3.1.db."""
    target = DB_PATH.parent / f"gearsmith-backup-{label}.db"
    if target.exists():
        target = DB_PATH.parent / f"gearsmith-backup-{label}-{datetime.now():%Y%m%d%H%M%S}.db"
    dest = sqlite3.connect(target)
    try:
        c.backup(dest)
    finally:
        dest.close()
    return target


def seed_example(c) -> None:
    """Fake demo gear so a fresh install shows what the app does. Never real data."""
    stamp = now_iso()

    def add_gear(type_, name, make, model, specs, interval=None, **kw):
        return c.execute(
            """INSERT INTO gear(type,name,make,model,specs,restring_interval_days,notes,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                type_, name, make, model, json.dumps(specs), interval,
                "Example entry - edit or delete it.", stamp, stamp,
            ),
        ).lastrowid

    starling = add_gear(
        "guitar", "Starling", "Demo Guitar Co", "Starling",
        {"finish": "3-tone sunburst", "pickups": "HH", "nut": "Bone", "scale": '25.5"',
         "tuning": "E standard", "string_gauge": "10-46", "mods": ""},
        interval=90, year=2019, serial="DG0001", status="home",
    )
    heron = add_gear(
        "guitar", "Heron", "Demo Guitar Co", "Heron",
        {"finish": "Natural", "pickups": "None (acoustic)", "nut": "Bone", "scale": '25.4"',
         "tuning": "E standard", "string_gauge": "12-53", "mods": ""},
        interval=120, year=2021, status="home",
    )
    club = add_gear(
        "amp", "Club 20", "Demo Amp Co", "Club 20",
        {"wattage": "20W", "speaker": '1x12"', "tubes": "2x EL84, 2x 12AX7"},
        serial="DA0042",
    )
    drive = add_gear(
        "pedal", "Demo Drive", "Demo Pedal Co", "Drive",
        {"voltage": "9V", "ma_draw": 15, "polarity": "Center negative", "bypass": "True bypass"},
    )
    delay = add_gear(
        "pedal", "Demo Delay", "Demo Pedal Co", "Delay",
        {"voltage": "9V", "ma_draw": 40, "polarity": "Center negative", "bypass": "Buffered"},
    )
    add_gear(
        "pick", "DemoPick 0.73", "DemoPick", "Standard",
        {"thickness": "0.73 mm", "material": "Nylon", "quantity": 12},
    )
    demo_strings = add_gear(
        "strings", "Demo Strings 10-46", "Demo Strings", "Regular",
        {"gauge": "10-46", "string_type": "electric", "material": "Nickel wound",
         "strings_per_set": 6, "sets_per_pack": 3},
    )
    c.execute("UPDATE gear SET strings_id=? WHERE id=?", (demo_strings, starling))
    c.execute("UPDATE gear SET sets_on_hand=3 WHERE id=?", (demo_strings,))

    # One guitar fresh, one overdue, so both chip states are visible.
    old = (today() - timedelta(days=100)).isoformat()
    recent = (today() - timedelta(days=20)).isoformat()
    c.execute(
        "INSERT INTO restrings(gear_id,brand,gauge,date,note,strings_id,created_at) VALUES(?,?,?,?,?,?,?)",
        (starling, "Demo Strings", "10-46", old, "Example restring.", demo_strings, stamp),
    )
    c.execute(
        "INSERT INTO restrings(gear_id,brand,gauge,date,note,created_at) VALUES(?,?,?,?,?,?)",
        (heron, "Demo Strings", "12-53", recent, "Example restring.", stamp),
    )

    set_id = c.execute(
        "INSERT INTO sets(name,notes,created_at,updated_at) VALUES(?,?,?,?)",
        ("Practice board", "Example set - edit or delete it.", stamp, stamp),
    ).lastrowid
    for sort, gid in enumerate([starling, club, drive, delay]):
        c.execute("INSERT INTO set_items(set_id,gear_id,sort) VALUES(?,?,?)", (set_id, gid, sort))


@app.on_event("startup")
def startup() -> None:
    init_db()
    if os.getenv("GEARSMITH_NOTIFY_WORKER", "true").lower() == "true":
        threading.Thread(target=notification_worker, daemon=True).start()


# ---------------------------------------------------------------- auth


def clean_username(value: str) -> str:
    value = (value or "").strip()
    if not (3 <= len(value) <= 40) or not all(ch.isalnum() or ch in "._-" for ch in value):
        raise HTTPException(
            400,
            "Username must be 3-40 characters using letters, numbers, dots, dashes, or underscores",
        )
    return value


def password_record(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    if len(password or "") < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return digest.hex(), salt.hex()


def verify_password(password: str, expected: str, salt: str) -> bool:
    try:
        digest, _ = password_record(password, salt)
    except HTTPException:
        return False
    return hmac.compare_digest(digest, expected)


def public_user(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "active": bool(row["active"]),
    }


def current_user(request: Request, admin: bool = False) -> sqlite3.Row:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Not signed in")
    with db() as c:
        row = c.execute(
            """SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires_at>? AND u.active=1""",
            (hashlib.sha256(token.encode()).hexdigest(), now_iso()),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Session expired")
    if admin and not row["is_admin"]:
        raise HTTPException(403, "Administrator access required")
    return row


def set_session(response: Response, user_id: int) -> None:
    raw = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    with db() as c:
        c.execute("DELETE FROM sessions WHERE expires_at<=?", (now_iso(),))
        c.execute(
            "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
            (hashlib.sha256(raw.encode()).hexdigest(), user_id, expires.isoformat(), now_iso()),
        )
    response.set_cookie(
        COOKIE,
        raw,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="strict",
        secure=os.getenv("GEARSMITH_COOKIE_SECURE", "false").lower() == "true",
        path="/",
    )


def token_auth(request: Request) -> tuple[sqlite3.Row, sqlite3.Row]:
    """Authenticate a bearer API token. Returns (token, owning user)."""
    auth = request.headers.get("authorization", "")
    ip = client_ip(request)
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    token_hash = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
    with db() as c:
        row = c.execute(
            "SELECT * FROM api_tokens WHERE token_hash=? AND revoked=0", (token_hash,)
        ).fetchone()
        user = (
            c.execute("SELECT * FROM users WHERE id=? AND active=1", (row["created_by"],)).fetchone()
            if row
            else None
        )
    if not row:
        with _api_lock:
            retry = _window(_api_failures[ip], API_FAIL_LIMIT, API_FAIL_WINDOW_SECONDS, False)
            if not retry:
                _api_failures[ip].append(time.monotonic())
        if retry:
            raise HTTPException(
                429, "Too many failed API attempts. Try again later.", headers={"Retry-After": str(retry)}
            )
        raise HTTPException(401, "Invalid API token", headers={"WWW-Authenticate": "Bearer"})
    if not user:
        raise HTTPException(403, "Token owner is inactive")
    with _api_lock:
        retry = _window(_api_calls[token_hash], API_LIMIT, API_WINDOW_SECONDS, True)
    if retry:
        raise HTTPException(
            429, "API rate limit exceeded. Try again later.", headers={"Retry-After": str(retry)}
        )
    with db() as c:
        c.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now_iso(), row["id"]))
    return row, user


class Credentials(BaseModel):
    username: str = Field(max_length=40)
    password: str = Field(max_length=200)


@app.get("/api/status")
def status():
    with db() as c:
        setup_required = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        name = get_setting(c, "app_name", "Gearsmith")
    return {"setup_required": setup_required, "app_name": name, "version": APP_VERSION}


@app.post("/api/setup")
def setup(body: Credentials, response: Response):
    username = clean_username(body.username)
    pw, salt = password_record(body.password)
    with db() as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise HTTPException(409, "Setup is already complete")
        uid = c.execute(
            "INSERT INTO users(username,password_hash,salt,is_admin,active,created_at) VALUES(?,?,?,1,1,?)",
            (username, pw, salt, now_iso()),
        ).lastrowid
        c.execute("UPDATE gear SET created_by=? WHERE created_by IS NULL", (uid,))
    set_session(response, uid)
    return {"ok": True}


@app.post("/api/login")
def login(body: Credentials, request: Request, response: Response):
    ip = client_ip(request)
    retry = login_retry_after(ip)
    if retry:
        raise HTTPException(
            429, "Too many login attempts. Try again later.", headers={"Retry-After": str(retry)}
        )
    with db() as c:
        row = c.execute(
            "SELECT * FROM users WHERE username=? COLLATE NOCASE", (body.username.strip(),)
        ).fetchone()
    if not row or not row["active"] or not verify_password(body.password, row["password_hash"], row["salt"]):
        record_login_failure(ip)
        raise HTTPException(401, "Invalid username or password")
    clear_login_failures(ip)
    set_session(response, row["id"])
    return {"user": public_user(row)}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        with db() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    return public_user(current_user(request))


# ---------------------------------------------------------------- optional features

# Hideable sections: each gear type, sets, and the maintenance loop.
FEATURES = ("guitars", "amps", "pedals", "picks", "strings", "sets", "maintenance", "songs", "want", "sold", "tuner")
FEATURE_LABELS = {
    "guitars": "Guitars section",
    "amps": "Amps section",
    "pedals": "Pedals section",
    "picks": "Picks section",
    "strings": "Strings section (string packs)",
    "sets": "Sets (rigs and boards)",
    "maintenance": "Maintenance (restring tracking)",
    "songs": "Songs (rig and tone settings per song)",
    "want": "Want list",
    "sold": "Sold archive",
}
FEATURE_FOR_TYPE = {"guitar": "guitars", "amp": "amps", "pedal": "pedals", "pick": "picks", "strings": "strings"}


def feature_on(c, name: str) -> bool:
    return get_setting(c, f"feature_{name}", "1") == "1"


# ---------------------------------------------------------------- gear


def check_url(value: str | None) -> str:
    v = (value or "").strip()
    if v and not re.match(r"^https?://", v, re.I):
        raise ValueError("Links must start with http:// or https://")
    return v


def clean_controls(value: Any) -> list[dict[str, str]]:
    """A gear item's control names, e.g. [{"name": "Gain", "kind": "knob"}], in panel order."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(400, "Controls must be a list")
    if len(value) > MAX_CONTROLS:
        raise HTTPException(400, f"At most {MAX_CONTROLS} controls")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            raise HTTPException(400, "Each control needs a name")
        name = str(item.get("name") or "").strip()[:40]
        if not name or name.lower() in seen:
            continue
        kind = str(item.get("kind") or "knob").strip().lower()
        if kind not in CONTROL_KINDS:
            raise HTTPException(400, "Control kind must be knob or switch")
        seen.add(name.lower())
        entry = {"name": name, "kind": kind}
        value = str(item.get("value") or "").strip()[:40]
        if value:
            entry["value"] = value
        out.append(entry)
    return out


def clean_specs(type_: str, specs: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only the fields that belong to this gear type, coerced to text or numbers."""
    out: dict[str, Any] = {}
    allowed = {key: (label, numeric) for key, label, numeric in SPEC_FIELDS[type_]}
    for key, value in (specs or {}).items():
        if key == "controls" and type_ in CONTROL_TYPES:
            out["controls"] = clean_controls(value)
            continue
        if key == "modeler" and type_ == "pedal":
            out["modeler"] = bool(value)
            continue
        if key not in allowed or value is None:
            continue
        label, numeric = allowed[key]
        choices = SPEC_CHOICES.get(type_, {}).get(key)
        if choices is not None:
            value = str(value).strip().lower()
            if value == "":
                continue
            if value not in choices:
                raise HTTPException(400, f"{label} must be one of: {', '.join(choices)}")
            out[key] = value
            continue
        if numeric:
            if value == "":
                continue
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                try:
                    out[key] = float(value)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{label} must be a number")
        else:
            out[key] = str(value)[:300]
    return out


def strings_info(c, gear_row, on: date | None = None) -> dict[str, Any] | None:
    """String age chip for a guitar: days since the last restring vs its interval."""
    if gear_row["type"] != "guitar":
        return None
    on = on or today()
    interval = gear_row["restring_interval_days"] or DEFAULT_RESTRING_INTERVAL
    row = c.execute(
        "SELECT date, brand, gauge, strings_id FROM restrings WHERE gear_id=? ORDER BY date DESC, id DESC LIMIT 1",
        (gear_row["id"],),
    ).fetchone()
    if not row:
        return {
            "days": None,
            "state": "never",
            "interval_days": interval,
            "last_date": None,
            "last_brand": "",
            "last_gauge": "",
            "last_strings": None,
            "due_date": None,
            "days_until_due": None,
        }
    last = iso_date(row["date"]) or on
    days = (on - last).days
    due = last + timedelta(days=interval)
    if days >= interval:
        state = "overdue"
    elif days >= round(interval * STRING_WARN_FRACTION):
        state = "aging"
    else:
        state = "fresh"
    return {
        "days": days,
        "state": state,
        "interval_days": interval,
        "last_date": row["date"],
        "last_brand": row["brand"],
        "last_gauge": row["gauge"],
        "last_strings": strings_ref(c, row["strings_id"]),
        "due_date": due.isoformat(),
        "days_until_due": (due - on).days,
    }


def strings_ref(c, strings_id: int | None) -> dict[str, Any] | None:
    """A short pointer to a strings item: enough to show and link it."""
    if strings_id is None:
        return None
    row = c.execute("SELECT * FROM gear WHERE id=? AND type='strings'", (strings_id,)).fetchone()
    if not row:
        return None
    specs = json.loads(row["specs"] or "{}")
    photos = photo_list(c, row["id"])
    return {
        "id": row["id"],
        "name": row["name"],
        "make": row["make"],
        "model": row["model"],
        "gauge": specs.get("gauge", ""),
        "string_type": specs.get("string_type", ""),
        "cover": photos[0]["url"] if photos else None,
    }


def strings_users(c, strings_id: int) -> list[dict[str, Any]]:
    """Guitars that use this strings item."""
    rows = c.execute(
        "SELECT id, name, lifecycle FROM gear WHERE strings_id=? AND type='guitar' ORDER BY name COLLATE NOCASE",
        (strings_id,),
    ).fetchall()
    return [{"id": r["id"], "name": r["name"], "lifecycle": r["lifecycle"]} for r in rows]


def check_strings_id(c, strings_id: int | None) -> int | None:
    if strings_id is None:
        return None
    row = c.execute("SELECT type FROM gear WHERE id=?", (strings_id,)).fetchone()
    if not row or row["type"] != "strings":
        raise HTTPException(400, "strings_id must be the id of a strings item")
    return strings_id


def display_user(c, user_id) -> str:
    if user_id is None:
        return ""
    row = c.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    return row[0] if row else "Former user"


def photo_list(c, gear_id: int) -> list[dict[str, Any]]:
    rows = c.execute(
        "SELECT id, filename FROM gear_photos WHERE gear_id=? ORDER BY sort, id", (gear_id,)
    ).fetchall()
    return [{"id": r["id"], "url": f"/api/photos/{r['filename']}"} for r in rows]


def gear_sets(c, gear_id: int) -> list[dict[str, Any]]:
    rows = c.execute(
        """SELECT s.id, s.name FROM sets s JOIN set_items si ON si.set_id=s.id
        WHERE si.gear_id=? ORDER BY s.name""",
        (gear_id,),
    ).fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def stock_state(row) -> str | None:
    """Strings supply at a glance: untracked, out, one left, or fine."""
    if row["sets_on_hand"] is None:
        return None
    if row["sets_on_hand"] <= 0:
        return "out"
    if row["sets_on_hand"] == 1:
        return "low"
    return "ok"


def gear_dict(c, row, on: date | None = None) -> dict[str, Any]:
    specs = json.loads(row["specs"] or "{}")
    photos = photo_list(c, row["id"])
    return {
        "id": row["id"],
        "type": row["type"],
        "type_label": GEAR_TYPE_LABELS[row["type"]],
        "type_singular": GEAR_TYPE_SINGULAR[row["type"]],
        "name": row["name"],
        "make": row["make"],
        "model": row["model"],
        "year": row["year"],
        "serial": row["serial"],
        "specs": specs,
        "spec_fields": [
            {"key": k, "label": label, "numeric": numeric, "value": specs.get(k)}
            for k, label, numeric in SPEC_FIELDS[row["type"]]
        ],
        "status": row["status"],
        "status_label": GEAR_STATUS_LABELS.get(row["status"], row["status"]),
        "purchase_date": row["purchase_date"],
        "purchase_price": row["purchase_price"],
        "notes": row["notes"],
        "restring_interval_days": row["restring_interval_days"],
        "favorite": bool(row["favorite"]),
        "lifecycle": row["lifecycle"],
        "lifecycle_label": LIFECYCLE_LABELS.get(row["lifecycle"], row["lifecycle"]),
        "want_price": row["want_price"],
        "sold_date": row["sold_date"],
        "sold_price": row["sold_price"],
        "manual_url": row["manual_url"],
        "sets_on_hand": row["sets_on_hand"] if row["type"] == "strings" else None,
        "stock_state": stock_state(row) if row["type"] == "strings" else None,
        "controls": specs.get("controls", []),
        "modeler": bool(specs.get("modeler")),
        "share": share_info(c, "gear", row["id"]),
        "strings": strings_info(c, row, on) if row["lifecycle"] == "owned" else None,
        "strings_id": row["strings_id"] if row["type"] == "guitar" else None,
        "strings_used": strings_ref(c, row["strings_id"]) if row["type"] == "guitar" else None,
        "used_on": strings_users(c, row["id"]) if row["type"] == "strings" else None,
        "photos": photos,
        "cover": photos[0]["url"] if photos else None,
        "sets": gear_sets(c, row["id"]),
        "added_by": display_user(c, row["created_by"]) or "System",
    }


def get_gear_row(c, gear_id: int):
    row = c.execute("SELECT * FROM gear WHERE id=?", (gear_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Gear not found")
    return row


class GearIn(BaseModel):
    type: Literal["guitar", "amp", "pedal", "pick", "strings"]
    name: str = Field(min_length=1, max_length=80)
    make: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=80)
    year: int | None = Field(default=None, ge=1900, le=2100)
    serial: str = Field(default="", max_length=80)
    specs: dict[str, Any] | None = None
    status: Literal["", "home", "luthier", "lent"] = ""
    purchase_date: str | None = None
    purchase_price: float | None = Field(default=None, ge=0, le=10_000_000)
    notes: str = Field(default="", max_length=4000)
    restring_interval_days: int | None = Field(default=None, ge=1, le=730)
    favorite: bool | None = None
    lifecycle: Literal["owned", "want", "sold"] | None = None
    want_price: float | None = Field(default=None, ge=0, le=10_000_000)
    sold_date: str | None = None
    sold_price: float | None = Field(default=None, ge=0, le=10_000_000)
    strings_id: int | None = Field(default=None, description="Guitars only: the strings item this guitar uses")
    sets_on_hand: int | None = Field(default=None, ge=0, le=999, description="Strings only: unopened sets in stock")
    manual_url: str = Field(default="", max_length=300, description="Link to the manual, shown as a button")

    @field_validator("purchase_date", "sold_date")
    @classmethod
    def _date(cls, v):
        return check_date(v)

    @field_validator("manual_url")
    @classmethod
    def _url(cls, v):
        return check_url(v)


class GearPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    make: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=80)
    year: int | None = Field(default=None, ge=1900, le=2100)
    serial: str | None = Field(default=None, max_length=80)
    specs: dict[str, Any] | None = None
    status: Literal["", "home", "luthier", "lent"] | None = None
    purchase_date: str | None = None
    purchase_price: float | None = Field(default=None, ge=0, le=10_000_000)
    notes: str | None = Field(default=None, max_length=4000)
    restring_interval_days: int | None = Field(default=None, ge=1, le=730)
    favorite: bool | None = None
    lifecycle: Literal["owned", "want", "sold"] | None = None
    want_price: float | None = Field(default=None, ge=0, le=10_000_000)
    sold_date: str | None = None
    sold_price: float | None = Field(default=None, ge=0, le=10_000_000)
    strings_id: int | None = Field(default=None, description="Guitars only: the strings item this guitar uses")
    sets_on_hand: int | None = Field(default=None, ge=0, le=999, description="Strings only: unopened sets in stock")
    manual_url: str | None = Field(default=None, max_length=300, description="Link to the manual, shown as a button")

    @field_validator("purchase_date", "sold_date")
    @classmethod
    def _date(cls, v):
        return check_date(v)

    @field_validator("manual_url")
    @classmethod
    def _url(cls, v):
        return check_url(v) if v is not None else v


def create_gear(c, body: GearIn, user_id: int | None) -> int:
    if body.type != "guitar" and body.restring_interval_days is not None:
        raise HTTPException(400, "Only guitars have a restring interval")
    if body.type != "guitar" and body.strings_id is not None:
        raise HTTPException(400, "Only guitars use a strings item")
    if body.type != "strings" and body.sets_on_hand is not None:
        raise HTTPException(400, "Only strings track sets on hand")
    check_strings_id(c, body.strings_id)
    stamp = now_iso()
    return c.execute(
        """INSERT INTO gear(type,name,make,model,year,serial,specs,status,purchase_date,
           purchase_price,notes,restring_interval_days,favorite,lifecycle,want_price,sold_date,
           sold_price,sets_on_hand,manual_url,strings_id,created_by,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            body.type, body.name.strip(), body.make.strip(), body.model.strip(), body.year,
            body.serial.strip(), json.dumps(clean_specs(body.type, body.specs)), body.status,
            body.purchase_date, body.purchase_price, body.notes.strip(), body.restring_interval_days,
            int(bool(body.favorite)), body.lifecycle or "owned", body.want_price, body.sold_date,
            body.sold_price, body.sets_on_hand, body.manual_url, body.strings_id, user_id, stamp, stamp,
        ),
    ).lastrowid


def patch_gear(c, row, body: GearPatch) -> None:
    data = body.model_dump(exclude_unset=True)
    specs = json.loads(row["specs"] or "{}")
    if "specs" in data:
        specs.update(clean_specs(row["type"], data.pop("specs")))
    data["specs"] = json.dumps(specs)
    if row["type"] != "guitar" and data.get("restring_interval_days") is not None:
        raise HTTPException(400, "Only guitars have a restring interval")
    if "strings_id" in data:
        if row["type"] != "guitar":
            if data["strings_id"] is not None:
                raise HTTPException(400, "Only guitars use a strings item")
            data.pop("strings_id")
        else:
            check_strings_id(c, data["strings_id"])
    if "sets_on_hand" in data:
        # null means "leave it alone", same rule as favorite
        value = data.pop("sets_on_hand")
        if value is not None:
            if row["type"] != "strings":
                raise HTTPException(400, "Only strings track sets on hand")
            data["sets_on_hand"] = value
    if "manual_url" in data and data["manual_url"] is None:
        # null leaves the link alone
        data.pop("manual_url")
    if "favorite" in data:
        # null means "leave it alone", so a full PUT from an older client never clears the star
        favorite = data.pop("favorite")
        if favorite is not None:
            data["favorite"] = int(favorite)
    if "lifecycle" in data and data["lifecycle"] is None:
        # same rule as favorite: null leaves the lifecycle alone
        data.pop("lifecycle")
    if "name" in data:
        data["name"] = data["name"].strip()
    for key in ("make", "model", "serial", "notes"):
        if key in data and data[key] is not None:
            data[key] = data[key].strip()
    data["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in data)
    c.execute(f"UPDATE gear SET {cols} WHERE id=?", (*data.values(), row["id"]))


def list_gear_rows(
    c, type_: str | None = None, lifecycle: str | None = None, q: str | None = None
) -> list[dict[str, Any]]:
    where, params = [], []
    if type_:
        if type_ not in GEAR_TYPES:
            raise HTTPException(400, "Unknown gear type")
        where.append("type=?")
        params.append(type_)
    if lifecycle:
        if lifecycle not in LIFECYCLES:
            raise HTTPException(400, "Lifecycle must be owned, want, or sold")
        where.append("lifecycle=?")
        params.append(lifecycle)
    if q and q.strip():
        # name, make, model, notes and spec values (gauge, string type, material...)
        needle = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append(
            "(name LIKE ? ESCAPE '\\' OR make LIKE ? ESCAPE '\\' OR model LIKE ? ESCAPE '\\'"
            " OR notes LIKE ? ESCAPE '\\' OR specs LIKE ? ESCAPE '\\')"
        )
        params += [needle] * 5
    sql = "SELECT * FROM gear"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY type, favorite DESC, name COLLATE NOCASE"
    return [gear_dict(c, r) for r in c.execute(sql, params)]


@app.get("/api/gear")
def list_gear(request: Request, type: str | None = None, lifecycle: str | None = None, q: str | None = None):
    current_user(request)
    with db() as c:
        return list_gear_rows(c, type, lifecycle, q)


@app.get("/api/gear/{gear_id}")
def get_gear(gear_id: int, request: Request):
    current_user(request)
    with db() as c:
        return gear_dict(c, get_gear_row(c, gear_id))


@app.post("/api/gear", status_code=201)
def add_gear(body: GearIn, request: Request):
    user = current_user(request)
    with db() as c:
        gear_id = create_gear(c, body, user["id"])
        return gear_dict(c, get_gear_row(c, gear_id))


@app.put("/api/gear/{gear_id}")
def replace_gear(gear_id: int, body: GearIn, request: Request):
    current_user(request)
    with db() as c:
        row = get_gear_row(c, gear_id)
        if body.type != row["type"]:
            raise HTTPException(400, "Gear type can't change; delete and re-add instead")
        # A PUT that doesn't mention strings_id (an older client) leaves the guitar's strings alone.
        skip = {"type"} if "strings_id" in body.model_fields_set else {"type", "strings_id"}
        patch = GearPatch(**body.model_dump(exclude=skip))
        patch_gear(c, row, patch)
        return gear_dict(c, get_gear_row(c, gear_id))


@app.patch("/api/gear/{gear_id}")
def update_gear(gear_id: int, body: GearPatch, request: Request):
    current_user(request)
    with db() as c:
        patch_gear(c, get_gear_row(c, gear_id), body)
        return gear_dict(c, get_gear_row(c, gear_id))


def remove_gear(c, gear_id: int) -> dict[str, Any]:
    row = get_gear_row(c, gear_id)
    for photo in photo_list(c, gear_id):
        unlink_photo(photo["url"].rsplit("/", 1)[-1])
    if row["type"] == "strings":
        # Guitars and restring entries keep their text; they just stop pointing here.
        c.execute("UPDATE gear SET strings_id=NULL WHERE strings_id=?", (gear_id,))
        c.execute("UPDATE restrings SET strings_id=NULL WHERE strings_id=?", (gear_id,))
    c.execute("DELETE FROM gear WHERE id=?", (row["id"],))
    return {"ok": True}


@app.delete("/api/gear/{gear_id}")
def delete_gear(gear_id: int, request: Request):
    current_user(request)
    with db() as c:
        return remove_gear(c, gear_id)


# ---------------------------------------------------------------- restring log


class RestringIn(BaseModel):
    brand: str = Field(default="", max_length=80)
    gauge: str = Field(default="", max_length=40)
    date: str | None = None
    note: str = Field(default="", max_length=1000)
    strings_id: int | None = Field(
        default=None,
        description="Optional strings item that went on. Blank brand and gauge are filled from it, "
        "and the guitar's strings switch to it.",
    )

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        return check_date(v)


def restring_dict(c, row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "gear_id": row["gear_id"],
        "brand": row["brand"],
        "gauge": row["gauge"],
        "date": row["date"],
        "note": row["note"],
        "strings_id": row["strings_id"],
        "strings": strings_ref(c, row["strings_id"]),
        "logged_by": display_user(c, row["user_id"]) or "System",
        "via": row["via"],
    }


def log_restring(c, gear_id: int, body: RestringIn, user_id: int | None, via: str = "") -> int:
    row = get_gear_row(c, gear_id)
    if row["type"] != "guitar":
        raise HTTPException(400, "Only guitars can be restrung")
    when = iso_date(body.date) if body.date else today()
    if when is None:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    if when > today():
        raise HTTPException(400, "A restring can't be logged in the future")
    brand, gauge = body.brand.strip(), body.gauge.strip()
    strings = strings_ref(c, check_strings_id(c, body.strings_id))
    if strings:
        # Picked from your strings: fill in whatever was left blank.
        brand = brand or strings["make"] or strings["name"]
        gauge = gauge or strings["gauge"]
    rid = c.execute(
        """INSERT INTO restrings(gear_id,brand,gauge,date,note,user_id,via,strings_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (gear_id, brand, gauge, when.isoformat(), body.note.strip(),
         user_id, via, body.strings_id, now_iso()),
    ).lastrowid
    # A fresh set of strings usually means the current gauge changed too.
    if gauge:
        specs = json.loads(row["specs"] or "{}")
        specs["string_gauge"] = gauge
        c.execute("UPDATE gear SET specs=? WHERE id=?", (json.dumps(specs), gear_id))
    if body.strings_id is not None:
        c.execute("UPDATE gear SET strings_id=? WHERE id=?", (body.strings_id, gear_id))
        # One set comes out of stock; it never dips below zero. Deleting the entry later
        # does not put it back - stock is adjusted by hand on the strings page.
        c.execute(
            "UPDATE gear SET sets_on_hand=MAX(sets_on_hand - 1, 0) WHERE id=? AND sets_on_hand IS NOT NULL",
            (body.strings_id,),
        )
    c.execute("UPDATE gear SET updated_at=? WHERE id=?", (now_iso(), gear_id))
    return rid


@app.get("/api/gear/{gear_id}/restrings")
def list_restrings(gear_id: int, request: Request):
    current_user(request)
    with db() as c:
        get_gear_row(c, gear_id)
        rows = c.execute(
            "SELECT * FROM restrings WHERE gear_id=? ORDER BY date DESC, id DESC", (gear_id,)
        ).fetchall()
        return [restring_dict(c, r) for r in rows]


@app.post("/api/gear/{gear_id}/restrings", status_code=201)
def add_restring(gear_id: int, body: RestringIn, request: Request):
    user = current_user(request)
    with db() as c:
        rid = log_restring(c, gear_id, body, user["id"])
        row = c.execute("SELECT * FROM restrings WHERE id=?", (rid,)).fetchone()
        return restring_dict(c, row)


@app.delete("/api/restrings/{restring_id}")
def delete_restring(restring_id: int, request: Request):
    current_user(request)
    with db() as c:
        row = c.execute("SELECT * FROM restrings WHERE id=?", (restring_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Restring not found")
        c.execute("DELETE FROM restrings WHERE id=?", (restring_id,))
        return {"ok": True}


# ---------------------------------------------------------------- maintenance log
# A per-item service history: setups, tube swaps, fret work, repairs, anything else.
# Restrings keep their own log above.

MAINTENANCE_CATEGORIES = ("setup", "tubes", "fret work", "repair", "other")


class MaintenanceIn(BaseModel):
    date: str | None = None
    category: Literal["setup", "tubes", "fret work", "repair", "other"] = "other"
    note: str = Field(default="", max_length=1000)

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        return check_date(v)


class MaintenancePatch(BaseModel):
    date: str | None = None
    category: Literal["setup", "tubes", "fret work", "repair", "other"] | None = None
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        return check_date(v)


def maintenance_dict(c, row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "gear_id": row["gear_id"],
        "date": row["date"],
        "category": row["category"],
        "note": row["note"],
        "logged_by": display_user(c, row["user_id"]) or "System",
        "created_at": row["created_at"],
    }


def get_maintenance_row(c, entry_id: int):
    row = c.execute("SELECT * FROM maintenance WHERE id=?", (entry_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Maintenance entry not found")
    return row


def maintenance_list(c, gear_id: int) -> list[dict[str, Any]]:
    get_gear_row(c, gear_id)
    rows = c.execute(
        "SELECT * FROM maintenance WHERE gear_id=? ORDER BY date DESC, id DESC", (gear_id,)
    ).fetchall()
    return [maintenance_dict(c, r) for r in rows]


def log_maintenance(c, gear_id: int, body: MaintenanceIn, user_id: int | None) -> int:
    get_gear_row(c, gear_id)
    when = iso_date(body.date) if body.date else today()
    if when is None:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    if when > today():
        raise HTTPException(400, "Maintenance can't be logged in the future")
    return c.execute(
        "INSERT INTO maintenance(gear_id,date,category,note,user_id,created_at) VALUES(?,?,?,?,?,?)",
        (gear_id, when.isoformat(), body.category, body.note.strip(), user_id, now_iso()),
    ).lastrowid


def edit_maintenance(c, entry_id: int, body: MaintenancePatch) -> dict[str, Any]:
    get_maintenance_row(c, entry_id)
    data = body.model_dump(exclude_unset=True)
    if "date" in data:
        if data["date"] is None:
            data.pop("date")
        else:
            when = iso_date(data["date"])
            if when is None:
                raise HTTPException(400, "Dates must be YYYY-MM-DD")
            if when > today():
                raise HTTPException(400, "Maintenance can't be logged in the future")
            data["date"] = when.isoformat()
    if "note" in data and data["note"] is not None:
        data["note"] = data["note"].strip()
    if data:
        cols = ", ".join(f"{k}=?" for k in data)
        c.execute(f"UPDATE maintenance SET {cols} WHERE id=?", (*data.values(), entry_id))
    return maintenance_dict(c, get_maintenance_row(c, entry_id))


def delete_maintenance_entry(c, entry_id: int) -> dict[str, Any]:
    get_maintenance_row(c, entry_id)
    c.execute("DELETE FROM maintenance WHERE id=?", (entry_id,))
    return {"ok": True}


@app.get("/api/gear/{gear_id}/maintenance")
def list_gear_maintenance(gear_id: int, request: Request):
    current_user(request)
    with db() as c:
        return maintenance_list(c, gear_id)


@app.post("/api/gear/{gear_id}/maintenance", status_code=201)
def add_maintenance(gear_id: int, body: MaintenanceIn, request: Request):
    user = current_user(request)
    with db() as c:
        entry_id = log_maintenance(c, gear_id, body, user["id"])
        return maintenance_dict(c, get_maintenance_row(c, entry_id))


@app.patch("/api/maintenance/{entry_id}")
def update_maintenance(entry_id: int, body: MaintenancePatch, request: Request):
    current_user(request)
    with db() as c:
        return edit_maintenance(c, entry_id, body)


@app.delete("/api/maintenance/{entry_id}")
def delete_maintenance(entry_id: int, request: Request):
    current_user(request)
    with db() as c:
        return delete_maintenance_entry(c, entry_id)


# ---------------------------------------------------------------- due list


def due_items(c, horizon: int = 7, on: date | None = None) -> list[dict[str, Any]]:
    """Guitars whose strings are overdue or coming due within `horizon` days."""
    on = on or today()
    out = []
    rows = c.execute(
        "SELECT * FROM gear WHERE type='guitar' AND lifecycle='owned' ORDER BY name COLLATE NOCASE"
    ).fetchall()
    for row in rows:
        info = strings_info(c, row, on)
        if not info or info["state"] == "never":
            continue
        overdue = info["state"] == "overdue"
        due_soon = not overdue and info["days_until_due"] is not None and info["days_until_due"] <= horizon
        if not (overdue or due_soon):
            continue
        out.append(
            {
                "gear_id": row["id"],
                "name": row["name"],
                "make": row["make"],
                "model": row["model"],
                "cover": gear_dict(c, row, on)["cover"],
                "state": "overdue" if overdue else "due",
                "days": info["days"],
                "days_until_due": info["days_until_due"],
                "interval_days": info["interval_days"],
                "last_date": info["last_date"],
                "due_date": info["due_date"],
            }
        )
    out.sort(key=lambda i: (i["days_until_due"] if i["days_until_due"] is not None else 0))
    return out


@app.get("/api/due")
def list_due(request: Request, days: int = 7):
    current_user(request)
    if days < 0 or days > 90:
        raise HTTPException(400, "Days must be 0-90")
    with db() as c:
        return due_items(c, days)


# ---------------------------------------------------------------- sets


def set_dict(c, row) -> dict[str, Any]:
    items = c.execute(
        """SELECT g.* FROM gear g JOIN set_items si ON si.gear_id=g.id
        WHERE si.set_id=? ORDER BY si.sort, g.name COLLATE NOCASE""",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "name": row["name"],
        "notes": row["notes"],
        "share": share_info(c, "set", row["id"]),
        "items": [
            {
                "id": g["id"],
                "type": g["type"],
                "name": g["name"],
                "make": g["make"],
                "model": g["model"],
                "cover": (lambda p: f"/api/photos/{p['filename']}" if p else None)(
                    c.execute(
                        "SELECT filename FROM gear_photos WHERE gear_id=? ORDER BY sort, id LIMIT 1",
                        (g["id"],),
                    ).fetchone()
                ),
            }
            for g in items
        ],
    }


def get_set_row(c, set_id: int):
    row = c.execute("SELECT * FROM sets WHERE id=?", (set_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Set not found")
    return row


class SetIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    notes: str = Field(default="", max_length=1000)
    item_ids: list[int] | None = None


class SetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    notes: str | None = Field(default=None, max_length=1000)
    item_ids: list[int] | None = None


def replace_set_items(c, set_id: int, item_ids: list[int]) -> None:
    seen: list[int] = []
    for gid in item_ids:
        if gid in seen:
            continue
        get_gear_row(c, gid)  # 404 on anything that isn't real gear
        seen.append(gid)
    c.execute("DELETE FROM set_items WHERE set_id=?", (set_id,))
    for sort, gid in enumerate(seen):
        c.execute("INSERT INTO set_items(set_id,gear_id,sort) VALUES(?,?,?)", (set_id, gid, sort))


@app.get("/api/sets")
def list_sets(request: Request):
    current_user(request)
    with db() as c:
        rows = c.execute("SELECT * FROM sets ORDER BY name COLLATE NOCASE").fetchall()
        return [set_dict(c, r) for r in rows]


@app.get("/api/sets/{set_id}")
def get_set(set_id: int, request: Request):
    current_user(request)
    with db() as c:
        return set_dict(c, get_set_row(c, set_id))


@app.post("/api/sets", status_code=201)
def add_set(body: SetIn, request: Request):
    current_user(request)
    stamp = now_iso()
    with db() as c:
        try:
            set_id = c.execute(
                "INSERT INTO sets(name,notes,created_at,updated_at) VALUES(?,?,?,?)",
                (body.name.strip(), body.notes.strip(), stamp, stamp),
            ).lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A set with that name already exists")
        if body.item_ids:
            replace_set_items(c, set_id, body.item_ids)
        return set_dict(c, get_set_row(c, set_id))


@app.patch("/api/sets/{set_id}")
def update_set(set_id: int, body: SetPatch, request: Request):
    current_user(request)
    with db() as c:
        get_set_row(c, set_id)
        data = body.model_dump(exclude_unset=True)
        item_ids = data.pop("item_ids", None)
        if data:
            if "name" in data:
                data["name"] = data["name"].strip()
            if "notes" in data and data["notes"] is not None:
                data["notes"] = data["notes"].strip()
            data["updated_at"] = now_iso()
            cols = ", ".join(f"{k}=?" for k in data)
            try:
                c.execute(f"UPDATE sets SET {cols} WHERE id=?", (*data.values(), set_id))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "A set with that name already exists")
        if item_ids is not None:
            replace_set_items(c, set_id, item_ids)
        return set_dict(c, get_set_row(c, set_id))


@app.delete("/api/sets/{set_id}")
def delete_set(set_id: int, request: Request):
    current_user(request)
    with db() as c:
        get_set_row(c, set_id)
        c.execute("DELETE FROM sets WHERE id=?", (set_id,))
        return {"ok": True}


# ---------------------------------------------------------------- photos


def photo_path(stored: str) -> Path:
    if not STORED_NAME_RE.match(stored):
        raise HTTPException(404, "Photo not found")
    return PHOTOS_DIR / stored


def unlink_photo(stored: str | None) -> None:
    if stored and STORED_NAME_RE.match(stored):
        try:
            (PHOTOS_DIR / stored).unlink()
        except FileNotFoundError:
            pass


def sniff_image(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftypmif1"):
        return "image/heic"
    return None


async def read_photo_upload(file: UploadFile) -> tuple[bytes, str]:
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(UPLOAD_CHUNK):
        total += len(chunk)
        if total > PHOTO_MAX_BYTES:
            raise HTTPException(413, "Photos must be 10 MB or smaller")
        chunks.append(chunk)
    data = b"".join(chunks)
    mime = sniff_image(data)
    if not mime:
        raise HTTPException(400, "That file doesn't look like a photo")
    return data, mime


async def save_photo(file: UploadFile) -> str:
    data, mime = await read_photo_upload(file)
    stored = secrets.token_hex(16) + PHOTO_TYPES[mime]
    await run_in_threadpool((PHOTOS_DIR / stored).write_bytes, data)
    return stored


async def attach_photo(c, gear_id: int, file: UploadFile) -> dict[str, Any]:
    get_gear_row(c, gear_id)
    stored = await save_photo(file)
    top = c.execute(
        "SELECT COALESCE(MAX(sort), -1) + 1 FROM gear_photos WHERE gear_id=?", (gear_id,)
    ).fetchone()[0]
    pid = c.execute(
        "INSERT INTO gear_photos(gear_id,filename,sort,created_at) VALUES(?,?,?,?)",
        (gear_id, stored, top, now_iso()),
    ).lastrowid
    c.execute("UPDATE gear SET updated_at=? WHERE id=?", (now_iso(), gear_id))
    return {"id": pid, "url": f"/api/photos/{stored}"}


@app.post("/api/gear/{gear_id}/photos", status_code=201)
async def add_photo(gear_id: int, request: Request, photo: UploadFile = File(...)):
    current_user(request)
    with db() as c:
        return await attach_photo(c, gear_id, photo)


def remove_photo(c, photo_id: int) -> dict[str, Any]:
    row = c.execute("SELECT * FROM gear_photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Photo not found")
    unlink_photo(row["filename"])
    c.execute("DELETE FROM gear_photos WHERE id=?", (photo_id,))
    c.execute("UPDATE gear SET updated_at=? WHERE id=?", (now_iso(), row["gear_id"]))
    return {"ok": True}


def set_cover(c, photo_id: int) -> dict[str, Any]:
    row = c.execute("SELECT * FROM gear_photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Photo not found")
    c.execute("UPDATE gear_photos SET sort=sort+1 WHERE gear_id=?", (row["gear_id"],))
    c.execute("UPDATE gear_photos SET sort=0 WHERE id=?", (photo_id,))
    c.execute("UPDATE gear SET updated_at=? WHERE id=?", (now_iso(), row["gear_id"]))
    return {"ok": True}


@app.delete("/api/photos/{photo_id}")
def delete_photo(photo_id: int, request: Request):
    current_user(request)
    with db() as c:
        return remove_photo(c, photo_id)


@app.post("/api/photos/{photo_id}/cover")
def make_cover(photo_id: int, request: Request):
    current_user(request)
    with db() as c:
        return set_cover(c, photo_id)


@app.get("/api/photos/{name}")
def get_photo(name: str, request: Request):
    current_user(request)
    path = photo_path(name)
    if not path.exists():
        raise HTTPException(404, "Photo not found")
    return FileResponse(path)


# ---------------------------------------------------------------- share links
# Optional read-only public links for one piece of gear or a whole set. The token is random
# and unguessable, pages are noindex/nofollow, links can expire, and deleting or regenerating
# a link kills the old URL right away.

SHARE_FAIL_LIMIT = 30
SHARE_FAIL_WINDOW_SECONDS = 15 * 60
SHARE_MAX_DAYS = 365
_share_failures: dict[str, deque[float]] = defaultdict(deque)
NOINDEX = "noindex, nofollow, noarchive, nosnippet, noimageindex"
SHARE_HEADERS = {"X-Robots-Tag": NOINDEX, "Cache-Control": "private, no-store"}


def share_info(c, kind: str, target_id: int) -> dict[str, Any] | None:
    col = "gear_id" if kind == "gear" else "set_id"
    row = c.execute(f"SELECT token, created_at, expires_at FROM shares WHERE {col}=?", (target_id,)).fetchone()
    if not row:
        return None
    expired = bool(row["expires_at"] and row["expires_at"] <= now_iso())
    return {
        "url": f"/share/{row['token']}",
        "token": row["token"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "expired": expired,
    }


class ShareIn(BaseModel):
    expires_in_days: int | None = Field(
        default=None, ge=1, le=SHARE_MAX_DAYS, description="Days until the link stops working; leave out for no expiry"
    )
    regenerate: bool = Field(default=False, description="Replace an existing link with a new URL")


def create_share(c, kind: str, target_id: int, user_id: int | None, body: ShareIn | None) -> dict[str, Any]:
    body = body or ShareIn()
    if kind == "gear":
        get_gear_row(c, target_id)
    else:
        get_set_row(c, target_id)
    col = "gear_id" if kind == "gear" else "set_id"
    existing = share_info(c, kind, target_id)
    expires = (
        (datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)).isoformat()
        if body.expires_in_days
        else None
    )
    if existing and not body.regenerate and not existing["expired"]:
        # Same URL, new expiry: lets you extend or clear the expiry without breaking the link.
        if "expires_in_days" in body.model_fields_set:
            c.execute(f"UPDATE shares SET expires_at=? WHERE {col}=?", (expires, target_id))
        return share_info(c, kind, target_id)
    c.execute(f"DELETE FROM shares WHERE {col}=?", (target_id,))
    c.execute(
        f"INSERT INTO shares(token,{col},expires_at,created_by,created_at) VALUES(?,?,?,?,?)",
        (secrets.token_urlsafe(24), target_id, expires, user_id, now_iso()),
    )
    return share_info(c, kind, target_id)


def revoke_share(c, kind: str, target_id: int) -> dict[str, Any]:
    if kind == "gear":
        get_gear_row(c, target_id)
    else:
        get_set_row(c, target_id)
    col = "gear_id" if kind == "gear" else "set_id"
    c.execute(f"DELETE FROM shares WHERE {col}=?", (target_id,))
    return {"ok": True}


def share_row(c, token: str, request: Request):
    """The live share for this token, or a plain 404 (unknown, revoked and expired look the same)."""
    ip = client_ip(request)
    with _api_lock:
        retry = _window(_share_failures[ip], SHARE_FAIL_LIMIT, SHARE_FAIL_WINDOW_SECONDS, False)
    if retry:
        raise HTTPException(429, "Too many requests", headers={"Retry-After": str(retry), **SHARE_HEADERS})
    row = None
    if re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token or ""):
        row = c.execute(
            "SELECT * FROM shares WHERE token=? AND (expires_at IS NULL OR expires_at>?)", (token, now_iso())
        ).fetchone()
    if not row:
        with _api_lock:
            _share_failures[ip].append(time.monotonic())
        raise HTTPException(404, "Not found", headers=SHARE_HEADERS)
    return row


def h(value: Any) -> str:
    return html_escape(str(value if value is not None else ""), quote=True)


def share_facts(row) -> list[tuple[str, Any]]:
    """What a shared page shows about an item: make, model, year and specs. Never the serial
    number, prices, or who added it."""
    specs = json.loads(row["specs"] or "{}")
    facts = [("Brand" if row["type"] in BRAND_TYPES else "Make", row["make"]), ("Model", row["model"]), ("Year", row["year"])]
    facts += [(label, specs.get(key)) for key, label, _ in SPEC_FIELDS[row["type"]]]
    return [(k, v) for k, v in facts if v not in (None, "")]


def share_photo_url(token: str, filename: str) -> str:
    return f"/share/{h(token)}/photos/{h(filename)}"


def share_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
<meta name="robots" content="{NOINDEX}" />
<meta name="referrer" content="no-referrer" />
<title>{h(title)}</title>
<link rel="stylesheet" href="/static/style.css" />
</head>
<body class="share-page">
<main class="wrap share">
{body}
<p class="share-foot">Shared read-only.</p>
</main>
</body>
</html>"""


def gear_share_html(c, row, token: str) -> str:
    photos = c.execute("SELECT filename FROM gear_photos WHERE gear_id=? ORDER BY sort, id", (row["id"],)).fetchall()
    photo_html = "".join(
        f'<img src="{share_photo_url(token, p["filename"])}" alt="Photo of {h(row["name"])}" />' for p in photos
    )
    facts = share_facts(row)
    fact_html = "".join(f'<div class="fact"><span>{h(k)}</span>{h(v)}</div>' for k, v in facts)
    parts = [
        f'<p class="share-type">{h(GEAR_TYPE_SINGULAR[row["type"]])}{" · sold" if row["lifecycle"] == "sold" else ""}</p>',
        f'<h1>{h(row["name"])}</h1>',
        f'<div class="share-photos">{photo_html}</div>' if photo_html else "",
        f'<div class="facts">{fact_html}</div>' if fact_html else "",
        f'<h2>Notes</h2><p class="notes">{h(row["notes"])}</p>' if row["notes"] else "",
    ]
    return share_shell(row["name"], "\n".join(p for p in parts if p))


def set_share_html(c, set_row, token: str) -> str:
    items = c.execute(
        """SELECT g.* FROM gear g JOIN set_items si ON si.gear_id=g.id
        WHERE si.set_id=? ORDER BY si.sort, g.name COLLATE NOCASE""",
        (set_row["id"],),
    ).fetchall()
    cards = []
    for g in items:
        cover = c.execute(
            "SELECT filename FROM gear_photos WHERE gear_id=? ORDER BY sort, id LIMIT 1", (g["id"],)
        ).fetchone()
        facts = share_facts(g)
        cards.append(
            f"""<section class="share-item">
  <div class="share-thumb">{f'<img src="{share_photo_url(token, cover["filename"])}" alt="" />' if cover else ""}</div>
  <div class="share-item-body">
    <p class="share-type">{h(GEAR_TYPE_SINGULAR[g["type"]])}</p>
    <h2>{h(g["name"])}</h2>
    {'<div class="facts">' + "".join(f'<div class="fact"><span>{h(k)}</span>{h(v)}</div>' for k, v in facts) + "</div>" if facts else ""}
    {f'<p class="notes">{h(g["notes"])}</p>' if g["notes"] else ""}
  </div>
</section>"""
        )
    parts = [
        '<p class="share-type">Set</p>',
        f'<h1>{h(set_row["name"])}</h1>',
        f'<p class="notes">{h(set_row["notes"])}</p>' if set_row["notes"] else "",
        f'<p class="muted">{len(items)} item{"s" if len(items) != 1 else ""}</p>',
        "".join(cards) or '<p class="muted">This set is empty.</p>',
    ]
    return share_shell(set_row["name"], "\n".join(p for p in parts if p))


@app.get("/share/{token}", include_in_schema=False)
def share_page(token: str, request: Request):
    with db() as c:
        share = share_row(c, token, request)
        if share["gear_id"]:
            body = gear_share_html(c, get_gear_row(c, share["gear_id"]), token)
        else:
            body = set_share_html(c, get_set_row(c, share["set_id"]), token)
    return HTMLResponse(body, headers=SHARE_HEADERS)


@app.get("/share/{token}/photos/{name}", include_in_schema=False)
def share_photo(token: str, name: str, request: Request):
    with db() as c:
        share = share_row(c, token, request)
        if share["gear_id"]:
            ok = c.execute(
                "SELECT 1 FROM gear_photos WHERE gear_id=? AND filename=?", (share["gear_id"], name)
            ).fetchone()
        else:
            ok = c.execute(
                """SELECT 1 FROM gear_photos p JOIN set_items si ON si.gear_id=p.gear_id
                WHERE si.set_id=? AND p.filename=?""",
                (share["set_id"], name),
            ).fetchone()
    path = photo_path(name)
    if not ok or not path.exists():
        raise HTTPException(404, "Not found", headers=SHARE_HEADERS)
    return FileResponse(path, headers=SHARE_HEADERS)


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


def register_share_routes(prefix: str, auth, v1: bool) -> None:
    extra: dict[str, Any] = {"tags": ["v1"]} if v1 else {"include_in_schema": False}
    tag = "v1_" if v1 else ""
    for kind, path, noun in (("gear", "/gear/{item_id}/share", "a piece of gear"), ("set", "/sets/{item_id}/share", "a set")):

        def make(kind=kind, path=path, noun=noun):
            @app.get(prefix + path, name=f"{tag}get_{kind}_share", summary=f"Get the share link for {noun}", **extra)
            def _get(item_id: int, request: Request):
                auth(request)
                with db() as c:
                    (get_gear_row if kind == "gear" else get_set_row)(c, item_id)
                    return {"share": share_info(c, kind, item_id)}

            @app.post(
                prefix + path, name=f"{tag}post_{kind}_share",
                summary=f"Create a read-only public link for {noun}. Returns the existing link unless "
                "regenerate is true; expires_in_days sets or changes the expiry",
                **extra,
            )
            def _post(item_id: int, request: Request, body: ShareIn | None = None):
                user = auth(request)
                with db() as c:
                    return {"share": create_share(c, kind, item_id, user["id"], body)}

            @app.delete(prefix + path, name=f"{tag}delete_{kind}_share",
                        summary=f"Turn off the share link for {noun} (the old URL stops working)", **extra)
            def _delete(item_id: int, request: Request):
                auth(request)
                with db() as c:
                    return revoke_share(c, kind, item_id)

        make()


# ---------------------------------------------------------------- songs
# Per-song rig and tone settings: which guitar, amp and set, the knob settings on each piece
# of gear in the chain, and patch pointers for modelers (multi-FX units).
# Gear links keep a copy of the gear's name so a song still reads right after the gear is sold
# or deleted. Knob values are always text ("2:00", "noon", "max", "Bright").

MAX_KNOBS = 40
MAX_RIG = 40
MAX_PATCHES = 20
MAX_BLOCKS = 40
MAX_SCENES = 16
MAX_SONG_PRESETS = 20
ENGAGED = ("on", "off", "toggle")
TUNING_SUGGESTIONS = ["E Std", "Eb", "D Std", "Drop D", "Drop C#", "Drop C", "DADGAD", "Open G", "Open D", "Open E"]


class Knob(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    value: str = Field(default="", max_length=40)

    @field_validator("value", mode="before")
    @classmethod
    def _text(cls, v):
        # knob positions are text by design: "2:00", "noon", "max", "7.5"
        return "" if v is None else str(v)


class RigSettingIn(BaseModel):
    gear_id: int | None = None
    gear_name: str | None = Field(default=None, max_length=80)
    position: int | None = Field(default=None, ge=0, le=1000)
    engaged: Literal["on", "off", "toggle"] = "on"
    knobs: list[Knob] = Field(default_factory=list, max_length=MAX_KNOBS)
    note: str = Field(default="", max_length=1000)


class RigSettingPatch(BaseModel):
    gear_id: int | None = None
    gear_name: str | None = Field(default=None, max_length=80)
    position: int | None = Field(default=None, ge=0, le=1000)
    engaged: Literal["on", "off", "toggle"] | None = None
    knobs: list[Knob] | None = Field(default=None, max_length=MAX_KNOBS)
    note: str | None = Field(default=None, max_length=1000)


class EffectBlockIn(BaseModel):
    slot: str = Field(default="", max_length=20)
    block_type: str = Field(default="", max_length=40)
    model: str = Field(default="", max_length=80)
    enabled: bool = True
    params: list[Knob] = Field(default_factory=list, max_length=MAX_KNOBS)
    scene_overrides: dict[str, Any] | None = None


class PatchIn(BaseModel):
    gear_id: int | None = None
    gear_name: str | None = Field(default=None, max_length=80)
    position: int | None = Field(default=None, ge=0, le=1000)
    patch_ref: str = Field(default="", max_length=40)
    patch_name: str = Field(default="", max_length=80)
    scenes: list[str] = Field(default_factory=list, max_length=MAX_SCENES)
    midi: dict[str, Any] | None = None
    note: str = Field(default="", max_length=1000)
    blocks: list[EffectBlockIn] | None = Field(default=None, max_length=MAX_BLOCKS)


class PatchPatch(BaseModel):
    gear_id: int | None = None
    gear_name: str | None = Field(default=None, max_length=80)
    position: int | None = Field(default=None, ge=0, le=1000)
    patch_ref: str | None = Field(default=None, max_length=40)
    patch_name: str | None = Field(default=None, max_length=80)
    scenes: list[str] | None = Field(default=None, max_length=MAX_SCENES)
    midi: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=1000)
    blocks: list[EffectBlockIn] | None = Field(default=None, max_length=MAX_BLOCKS)


class SongIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    artist: str = Field(default="", max_length=120)
    tuning: str = Field(default="", max_length=40)
    capo: int | None = Field(default=None, ge=0, le=24)
    key: str = Field(default="", max_length=20)
    bpm: int | None = Field(default=None, ge=1, le=400)
    guitar_id: int | None = None
    amp_id: int | None = None
    set_id: int | None = None
    notes: str = Field(default="", max_length=4000)
    rig: list[RigSettingIn] | None = Field(default=None, max_length=MAX_RIG)
    patches: list[PatchIn] | None = Field(default=None, max_length=MAX_PATCHES)
    presets: list["SongPresetIn"] | None = Field(default=None, max_length=MAX_SONG_PRESETS)


class SongPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    artist: str | None = Field(default=None, max_length=120)
    tuning: str | None = Field(default=None, max_length=40)
    capo: int | None = Field(default=None, ge=0, le=24)
    key: str | None = Field(default=None, max_length=20)
    bpm: int | None = Field(default=None, ge=1, le=400)
    guitar_id: int | None = None
    amp_id: int | None = None
    set_id: int | None = None
    notes: str | None = Field(default=None, max_length=4000)
    rig: list[RigSettingIn] | None = Field(default=None, max_length=MAX_RIG)
    patches: list[PatchIn] | None = Field(default=None, max_length=MAX_PATCHES)
    presets: list["SongPresetIn"] | None = Field(default=None, max_length=MAX_SONG_PRESETS)


class SongPresetIn(BaseModel):
    preset_id: int
    label: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=1000)


class PresetIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    artist: str = Field(default="", max_length=120)
    amp_id: int | None = None
    notes: str = Field(default="", max_length=4000)
    rig: list[RigSettingIn] | None = Field(default=None, max_length=MAX_RIG)
    patches: list[PatchIn] | None = Field(default=None, max_length=MAX_PATCHES)


class PresetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    artist: str | None = Field(default=None, max_length=120)
    amp_id: int | None = None
    notes: str | None = Field(default=None, max_length=4000)
    rig: list[RigSettingIn] | None = Field(default=None, max_length=MAX_RIG)
    patches: list[PatchIn] | None = Field(default=None, max_length=MAX_PATCHES)


class SaveAsPresetIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    use_in_song: bool = False


SongIn.model_rebuild()
SongPatch.model_rebuild()


class Owner(NamedTuple):
    """Where a signal chain lives: on a song, or on a reusable preset."""

    col: str
    rig: str
    patches: str
    blocks: str


SONG = Owner("song_id", "song_gear_settings", "song_device_patches", "song_effect_blocks")
PRESET = Owner("preset_id", "preset_gear_settings", "preset_device_patches", "preset_effect_blocks")


def knobs_json(knobs: list[Knob] | None) -> str:
    return json.dumps([{"name": k.name.strip(), "value": k.value.strip()} for k in (knobs or []) if k.name.strip()])


def gear_link(c, gear_id: int | None, want_type: str | None = None, name: str | None = None) -> tuple[int | None, str]:
    """Resolve a gear link to (id, copied name). A missing id keeps just the typed name."""
    if gear_id is None:
        return None, (name or "").strip()
    row = c.execute("SELECT id, type, name FROM gear WHERE id=?", (gear_id,)).fetchone()
    if not row:
        raise HTTPException(400, f"Gear {gear_id} not found")
    if want_type and row["type"] != want_type:
        raise HTTPException(400, f"Gear {gear_id} is not a {want_type}")
    return row["id"], row["name"]


def get_song_row(c, song_id: int):
    row = c.execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Song not found")
    return row


def rig_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "gear_id": row["gear_id"],
        "gear_name": row["gear_name"],
        "position": row["position"],
        "engaged": row["engaged"],
        "knobs": json.loads(row["knobs"] or "[]"),
        "note": row["note"],
    }


def block_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "position": row["position"],
        "slot": row["slot"],
        "block_type": row["block_type"],
        "model": row["model"],
        "enabled": bool(row["enabled"]),
        "params": json.loads(row["params"] or "[]"),
        "scene_overrides": json.loads(row["scene_overrides"]) if row["scene_overrides"] else None,
    }


def patch_dict(c, row, o: Owner = SONG) -> dict[str, Any]:
    blocks = c.execute(
        f"SELECT * FROM {o.blocks} WHERE patch_id=? ORDER BY position, id", (row["id"],)
    ).fetchall()
    return {
        "id": row["id"],
        "gear_id": row["gear_id"],
        "gear_name": row["gear_name"],
        "position": row["position"],
        "patch_ref": row["patch_ref"],
        "patch_name": row["patch_name"],
        "scenes": json.loads(row["scenes"] or "[]"),
        "midi": json.loads(row["midi"]) if row["midi"] else None,
        "note": row["note"],
        "blocks": [block_dict(b) for b in blocks],
    }


def song_photo_list(c, song_id: int) -> list[dict[str, Any]]:
    rows = c.execute(
        "SELECT id, filename FROM song_photos WHERE song_id=? ORDER BY sort, id", (song_id,)
    ).fetchall()
    return [{"id": r["id"], "url": f"/api/photos/{r['filename']}"} for r in rows]


def song_summary(c, row) -> dict[str, Any]:
    photos = song_photo_list(c, row["id"])
    return {
        "id": row["id"],
        "title": row["title"],
        "artist": row["artist"],
        "tuning": row["tuning"],
        "capo": row["capo"],
        "key": row["song_key"],
        "bpm": row["bpm"],
        "guitar_id": row["guitar_id"],
        "guitar_name": row["guitar_name"],
        "amp_id": row["amp_id"],
        "amp_name": row["amp_name"],
        "set_id": row["set_id"],
        "set_name": row["set_name"],
        "notes": row["notes"],
        "cover": photos[0]["url"] if photos else None,
        "preset_names": [
            r["name"] for r in c.execute(
                """SELECT p.name FROM song_presets sp JOIN presets p ON p.id=sp.preset_id
                WHERE sp.song_id=? ORDER BY sp.position, sp.id""",
                (row["id"],),
            )
        ],
        "added_by": display_user(c, row["created_by"]) or "System",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def chain_of(c, owner_id: int, o: Owner) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rig = c.execute(
        f"SELECT * FROM {o.rig} WHERE {o.col}=? ORDER BY position, id", (owner_id,)
    ).fetchall()
    patches = c.execute(
        f"SELECT * FROM {o.patches} WHERE {o.col}=? ORDER BY position, id", (owner_id,)
    ).fetchall()
    return [rig_dict(r) for r in rig], [patch_dict(c, p, o) for p in patches]


def song_dict(c, row) -> dict[str, Any]:
    out = song_summary(c, row)
    out["rig"], out["patches"] = chain_of(c, row["id"], SONG)
    out["presets"] = song_preset_list(c, row["id"])
    out["photos"] = song_photo_list(c, row["id"])
    return out


def song_links(c, data: dict[str, Any]) -> dict[str, Any]:
    """Turn guitar_id/amp_id/set_id in a payload into columns, with name copies."""
    cols: dict[str, Any] = {}
    if "guitar_id" in data:
        cols["guitar_id"], name = gear_link(c, data["guitar_id"], "guitar")
        if data["guitar_id"] is not None:
            cols["guitar_name"] = name
    if "amp_id" in data:
        cols["amp_id"], name = gear_link(c, data["amp_id"], "amp")
        if data["amp_id"] is not None:
            cols["amp_name"] = name
    if "set_id" in data:
        if data["set_id"] is None:
            cols["set_id"] = None
        else:
            row = get_set_row(c, data["set_id"])
            cols["set_id"], cols["set_name"] = row["id"], row["name"]
    return cols


def insert_rig_setting(c, owner_id: int, item: RigSettingIn, position: int, o: Owner = SONG) -> int:
    gear_id, name = gear_link(c, item.gear_id, None, item.gear_name)
    if not name:
        raise HTTPException(400, "Each rig entry needs gear or a gear name")
    return c.execute(
        f"""INSERT INTO {o.rig}({o.col},gear_id,gear_name,position,engaged,knobs,note)
        VALUES(?,?,?,?,?,?,?)""",
        (owner_id, gear_id, name, item.position if item.position is not None else position,
         item.engaged, knobs_json(item.knobs), item.note.strip()),
    ).lastrowid


def write_blocks(c, patch_id: int, blocks: list[EffectBlockIn], o: Owner = SONG) -> None:
    c.execute(f"DELETE FROM {o.blocks} WHERE patch_id=?", (patch_id,))
    for pos, b in enumerate(blocks):
        c.execute(
            f"""INSERT INTO {o.blocks}(patch_id,position,slot,block_type,model,enabled,params,scene_overrides)
            VALUES(?,?,?,?,?,?,?,?)""",
            (patch_id, pos, b.slot.strip(), b.block_type.strip(), b.model.strip(), int(b.enabled),
             knobs_json(b.params), json.dumps(b.scene_overrides) if b.scene_overrides else None),
        )


def clean_scenes(scenes: list[str] | None) -> str:
    return json.dumps([str(s).strip()[:40] for s in (scenes or []) if str(s).strip()])


def insert_patch(c, owner_id: int, item: PatchIn, position: int, o: Owner = SONG) -> int:
    gear_id, name = gear_link(c, item.gear_id, None, item.gear_name)
    if not name:
        raise HTTPException(400, "Each patch needs a device or a device name")
    pid = c.execute(
        f"""INSERT INTO {o.patches}({o.col},gear_id,gear_name,position,patch_ref,patch_name,scenes,midi,note)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (owner_id, gear_id, name, item.position if item.position is not None else position,
         item.patch_ref.strip(), item.patch_name.strip(), clean_scenes(item.scenes),
         json.dumps(item.midi) if item.midi else None, item.note.strip()),
    ).lastrowid
    if item.blocks:
        write_blocks(c, pid, item.blocks, o)
    return pid


def replace_rig(c, owner_id: int, rig: list[RigSettingIn], o: Owner = SONG) -> None:
    c.execute(f"DELETE FROM {o.rig} WHERE {o.col}=?", (owner_id,))
    for pos, item in enumerate(rig):
        insert_rig_setting(c, owner_id, item, pos, o)


def replace_patches(c, owner_id: int, patches: list[PatchIn], o: Owner = SONG) -> None:
    c.execute(f"DELETE FROM {o.patches} WHERE {o.col}=?", (owner_id,))
    for pos, item in enumerate(patches):
        insert_patch(c, owner_id, item, pos, o)


def create_song(c, body: SongIn, user_id: int | None) -> int:
    stamp = now_iso()
    data = body.model_dump(exclude={"rig", "patches"})
    links = song_links(c, {k: data[k] for k in ("guitar_id", "amp_id", "set_id")})
    song_id = c.execute(
        """INSERT INTO songs(title,artist,tuning,capo,song_key,bpm,guitar_id,guitar_name,amp_id,amp_name,
           set_id,set_name,notes,created_by,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            body.title.strip(), body.artist.strip(), body.tuning.strip(), body.capo, body.key.strip(), body.bpm,
            links.get("guitar_id"), links.get("guitar_name", ""), links.get("amp_id"), links.get("amp_name", ""),
            links.get("set_id"), links.get("set_name", ""), body.notes.strip(), user_id, stamp, stamp,
        ),
    ).lastrowid
    if body.rig:
        replace_rig(c, song_id, body.rig)
    if body.patches:
        replace_patches(c, song_id, body.patches)
    if body.presets:
        replace_song_presets(c, song_id, body.presets)
    return song_id


def update_song(c, row, body: SongPatch) -> None:
    data = body.model_dump(exclude_unset=True)
    rig = data.pop("rig", None)
    patches = data.pop("patches", None)
    presets = data.pop("presets", None)
    cols = song_links(c, {k: data.pop(k) for k in ("guitar_id", "amp_id", "set_id") if k in data})
    for key in ("title", "artist", "tuning", "key", "notes"):
        if key in data:
            if data[key] is None:
                if key == "title":
                    raise HTTPException(400, "Title is required")
                data[key] = ""
            cols["song_key" if key == "key" else key] = data.pop(key).strip()
    for key in ("capo", "bpm"):
        if key in data:
            cols[key] = data.pop(key)
    cols["updated_at"] = now_iso()
    sql = ", ".join(f"{k}=?" for k in cols)
    c.execute(f"UPDATE songs SET {sql} WHERE id=?", (*cols.values(), row["id"]))
    if rig is not None:
        replace_rig(c, row["id"], body.rig)
    if patches is not None:
        replace_patches(c, row["id"], body.patches)
    if presets is not None:
        replace_song_presets(c, row["id"], body.presets)


def touch_song(c, song_id: int) -> None:
    c.execute("UPDATE songs SET updated_at=? WHERE id=?", (now_iso(), song_id))


def list_song_rows(
    c, q: str | None = None, gear_id: int | None = None, artist: str | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM songs"
    where, params = [], []
    if q:
        where.append("(title LIKE ? OR artist LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if gear_id is not None:
        where.append(
            """(guitar_id=? OR amp_id=? OR id IN (SELECT song_id FROM song_gear_settings WHERE gear_id=?)
            OR id IN (SELECT song_id FROM song_device_patches WHERE gear_id=?)
            OR id IN (SELECT sp.song_id FROM song_presets sp JOIN presets p ON p.id=sp.preset_id
                      WHERE p.amp_id=?
                      OR p.id IN (SELECT preset_id FROM preset_gear_settings WHERE gear_id=?)
                      OR p.id IN (SELECT preset_id FROM preset_device_patches WHERE gear_id=?)))"""
        )
        params += [gear_id] * 7
    if artist is not None:
        where.append("LOWER(TRIM(artist))=LOWER(TRIM(?))")
        params.append(artist)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY title COLLATE NOCASE, artist COLLATE NOCASE"
    return [song_summary(c, r) for r in c.execute(sql, params)]


def remove_song(c, song_id: int) -> dict[str, Any]:
    get_song_row(c, song_id)
    for p in song_photo_list(c, song_id):
        unlink_photo(p["url"].rsplit("/", 1)[-1])
    c.execute("DELETE FROM songs WHERE id=?", (song_id,))
    return {"ok": True}


def rig_row(c, song_id: int, setting_id: int):
    row = c.execute(
        "SELECT * FROM song_gear_settings WHERE id=? AND song_id=?", (setting_id, song_id)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Rig entry not found")
    return row


def patch_row(c, song_id: int, patch_id: int):
    row = c.execute(
        "SELECT * FROM song_device_patches WHERE id=? AND song_id=?", (patch_id, song_id)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Patch not found")
    return row


def add_rig_setting(c, song_id: int, body: RigSettingIn) -> dict[str, Any]:
    get_song_row(c, song_id)
    top = c.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM song_gear_settings WHERE song_id=?", (song_id,)
    ).fetchone()[0]
    sid = insert_rig_setting(c, song_id, body, top)
    touch_song(c, song_id)
    return rig_dict(c.execute("SELECT * FROM song_gear_settings WHERE id=?", (sid,)).fetchone())


def edit_rig_setting(c, song_id: int, setting_id: int, body: RigSettingPatch) -> dict[str, Any]:
    row = rig_row(c, song_id, setting_id)
    data = body.model_dump(exclude_unset=True)
    cols: dict[str, Any] = {}
    if "gear_id" in data or "gear_name" in data:
        gid, name = gear_link(c, data.get("gear_id", row["gear_id"]), None, data.get("gear_name") or row["gear_name"])
        cols["gear_id"], cols["gear_name"] = gid, name or row["gear_name"]
    if data.get("position") is not None:
        cols["position"] = data["position"]
    if data.get("engaged") is not None:
        cols["engaged"] = data["engaged"]
    if data.get("knobs") is not None:
        cols["knobs"] = knobs_json(body.knobs)
    if data.get("note") is not None:
        cols["note"] = data["note"].strip()
    if cols:
        sql = ", ".join(f"{k}=?" for k in cols)
        c.execute(f"UPDATE song_gear_settings SET {sql} WHERE id=?", (*cols.values(), setting_id))
        touch_song(c, song_id)
    return rig_dict(c.execute("SELECT * FROM song_gear_settings WHERE id=?", (setting_id,)).fetchone())


def delete_rig_setting(c, song_id: int, setting_id: int) -> dict[str, Any]:
    rig_row(c, song_id, setting_id)
    c.execute("DELETE FROM song_gear_settings WHERE id=?", (setting_id,))
    touch_song(c, song_id)
    return {"ok": True}


def add_patch(c, song_id: int, body: PatchIn) -> dict[str, Any]:
    get_song_row(c, song_id)
    top = c.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM song_device_patches WHERE song_id=?", (song_id,)
    ).fetchone()[0]
    pid = insert_patch(c, song_id, body, top)
    touch_song(c, song_id)
    return patch_dict(c, c.execute("SELECT * FROM song_device_patches WHERE id=?", (pid,)).fetchone())


def edit_patch(c, song_id: int, patch_id: int, body: PatchPatch) -> dict[str, Any]:
    row = patch_row(c, song_id, patch_id)
    data = body.model_dump(exclude_unset=True)
    cols: dict[str, Any] = {}
    if "gear_id" in data or "gear_name" in data:
        gid, name = gear_link(c, data.get("gear_id", row["gear_id"]), None, data.get("gear_name") or row["gear_name"])
        cols["gear_id"], cols["gear_name"] = gid, name or row["gear_name"]
    for key in ("patch_ref", "patch_name", "note"):
        if data.get(key) is not None:
            cols[key] = data[key].strip()
    if data.get("position") is not None:
        cols["position"] = data["position"]
    if data.get("scenes") is not None:
        cols["scenes"] = clean_scenes(data["scenes"])
    if "midi" in data:
        cols["midi"] = json.dumps(data["midi"]) if data["midi"] else None
    if cols:
        sql = ", ".join(f"{k}=?" for k in cols)
        c.execute(f"UPDATE song_device_patches SET {sql} WHERE id=?", (*cols.values(), patch_id))
    if body.blocks is not None:
        write_blocks(c, patch_id, body.blocks)
    touch_song(c, song_id)
    return patch_dict(c, c.execute("SELECT * FROM song_device_patches WHERE id=?", (patch_id,)).fetchone())


def delete_patch(c, song_id: int, patch_id: int) -> dict[str, Any]:
    patch_row(c, song_id, patch_id)
    c.execute("DELETE FROM song_device_patches WHERE id=?", (patch_id,))
    touch_song(c, song_id)
    return {"ok": True}


async def attach_song_photo(c, song_id: int, file: UploadFile) -> dict[str, Any]:
    get_song_row(c, song_id)
    stored = await save_photo(file)
    top = c.execute(
        "SELECT COALESCE(MAX(sort), -1) + 1 FROM song_photos WHERE song_id=?", (song_id,)
    ).fetchone()[0]
    pid = c.execute(
        "INSERT INTO song_photos(song_id,filename,sort,created_at) VALUES(?,?,?,?)",
        (song_id, stored, top, now_iso()),
    ).lastrowid
    touch_song(c, song_id)
    return {"id": pid, "url": f"/api/photos/{stored}"}


def song_photo_row(c, photo_id: int):
    row = c.execute("SELECT * FROM song_photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Photo not found")
    return row


def remove_song_photo(c, photo_id: int) -> dict[str, Any]:
    row = song_photo_row(c, photo_id)
    unlink_photo(row["filename"])
    c.execute("DELETE FROM song_photos WHERE id=?", (photo_id,))
    touch_song(c, row["song_id"])
    return {"ok": True}


def set_song_cover(c, photo_id: int) -> dict[str, Any]:
    row = song_photo_row(c, photo_id)
    c.execute("UPDATE song_photos SET sort=sort+1 WHERE song_id=?", (row["song_id"],))
    c.execute("UPDATE song_photos SET sort=0 WHERE id=?", (photo_id,))
    touch_song(c, row["song_id"])
    return {"ok": True}


# ---------------------------------------------------------------- presets


def get_preset_row(c, preset_id: int):
    row = c.execute("SELECT * FROM presets WHERE id=?", (preset_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Preset not found")
    return row


def preset_song_count(c, preset_id: int) -> int:
    return c.execute(
        "SELECT COUNT(DISTINCT song_id) FROM song_presets WHERE preset_id=?", (preset_id,)
    ).fetchone()[0]


def preset_summary(c, row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "artist": row["artist"],
        "amp_id": row["amp_id"],
        "amp_name": row["amp_name"],
        "notes": row["notes"],
        "song_count": preset_song_count(c, row["id"]),
        "chain_summary": chain_summary(c, row["id"], PRESET),
        "added_by": display_user(c, row["created_by"]) or "System",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def chain_summary(c, owner_id: int, o: Owner) -> list[str]:
    """Short names for a chain in signal order, for list cards.

    Gear settings give the gear's name. A modeler patch gives its enabled block models,
    or the patch name (or device name) when it has no blocks.
    """
    names: list[str] = []
    for r in c.execute(
        f"SELECT gear_name FROM {o.rig} WHERE {o.col}=? ORDER BY position, id", (owner_id,)
    ).fetchall():
        names.append(r["gear_name"])
    for p in c.execute(
        f"SELECT id, gear_name, patch_name FROM {o.patches} WHERE {o.col}=? ORDER BY position, id",
        (owner_id,),
    ).fetchall():
        models = [
            b["model"]
            for b in c.execute(
                f"SELECT model FROM {o.blocks} WHERE patch_id=? AND enabled=1 ORDER BY position, id",
                (p["id"],),
            ).fetchall()
            if b["model"]
        ]
        names.extend(models or [p["patch_name"] or p["gear_name"]])
    return [n for n in names if n]


def preset_dict(c, row, with_songs: bool = True) -> dict[str, Any]:
    out = preset_summary(c, row)
    out["rig"], out["patches"] = chain_of(c, row["id"], PRESET)
    if with_songs:
        songs = c.execute(
            """SELECT DISTINCT s.* FROM songs s JOIN song_presets sp ON sp.song_id=s.id
            WHERE sp.preset_id=? ORDER BY s.title COLLATE NOCASE""",
            (row["id"],),
        ).fetchall()
        out["songs"] = [{"id": s["id"], "title": s["title"], "artist": s["artist"]} for s in songs]
    return out


def song_preset_list(c, song_id: int) -> list[dict[str, Any]]:
    """Presets used by a song, each with its full, current chain (a live link, not a copy)."""
    rows = c.execute(
        """SELECT sp.*, p.id AS pid FROM song_presets sp JOIN presets p ON p.id=sp.preset_id
        WHERE sp.song_id=? ORDER BY sp.position, sp.id""",
        (song_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "preset_id": r["preset_id"],
            "label": r["label"],
            "note": r["note"],
            "position": r["position"],
            "preset": preset_dict(c, get_preset_row(c, r["pid"]), with_songs=False),
        }
        for r in rows
    ]


def replace_song_presets(c, song_id: int, items: list[SongPresetIn]) -> None:
    c.execute("DELETE FROM song_presets WHERE song_id=?", (song_id,))
    for pos, item in enumerate(items):
        get_preset_row(c, item.preset_id)
        c.execute(
            "INSERT INTO song_presets(song_id,preset_id,position,label,note) VALUES(?,?,?,?,?)",
            (song_id, item.preset_id, pos, item.label.strip(), item.note.strip()),
        )


def list_preset_rows(c, q: str | None = None, artist: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM presets"
    where, params = [], []
    if q:
        where.append("(name LIKE ? OR artist LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if artist is not None:
        where.append("LOWER(TRIM(artist))=LOWER(TRIM(?))")
        params.append(artist)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name COLLATE NOCASE"
    return [preset_summary(c, r) for r in c.execute(sql, params)]


def create_preset(c, body: PresetIn, user_id: int | None) -> int:
    stamp = now_iso()
    amp_id, amp_name = gear_link(c, body.amp_id, "amp")
    preset_id = c.execute(
        """INSERT INTO presets(name,artist,amp_id,amp_name,notes,created_by,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        (body.name.strip(), body.artist.strip(), amp_id, amp_name, body.notes.strip(), user_id, stamp, stamp),
    ).lastrowid
    if body.rig:
        replace_rig(c, preset_id, body.rig, PRESET)
    if body.patches:
        replace_patches(c, preset_id, body.patches, PRESET)
    return preset_id


def update_preset(c, row, body: PresetPatch) -> None:
    data = body.model_dump(exclude_unset=True)
    cols: dict[str, Any] = {}
    if "name" in data:
        if not (data["name"] or "").strip():
            raise HTTPException(400, "Name is required")
        cols["name"] = data["name"].strip()
    for key in ("artist", "notes"):
        if key in data:
            cols[key] = (data[key] or "").strip()
    if "amp_id" in data:
        cols["amp_id"], name = gear_link(c, data["amp_id"], "amp")
        if data["amp_id"] is not None:
            cols["amp_name"] = name
    cols["updated_at"] = now_iso()
    sql = ", ".join(f"{k}=?" for k in cols)
    c.execute(f"UPDATE presets SET {sql} WHERE id=?", (*cols.values(), row["id"]))
    if body.rig is not None:
        replace_rig(c, row["id"], body.rig, PRESET)
    if body.patches is not None:
        replace_patches(c, row["id"], body.patches, PRESET)


def copy_chain(c, src_id: int, src: Owner, dst_id: int, dst: Owner) -> None:
    """Append one chain (rig + patches) onto another, as independent rows."""
    rig, patches = chain_of(c, src_id, src)
    top = c.execute(f"SELECT COALESCE(MAX(position), -1) + 1 FROM {dst.rig} WHERE {dst.col}=?", (dst_id,)).fetchone()[0]
    for i, r in enumerate(rig):
        item = RigSettingIn(gear_name=r["gear_name"], engaged=r["engaged"], note=r["note"],
                            knobs=[Knob(**k) for k in r["knobs"]])
        item.gear_id = r["gear_id"]
        insert_rig_setting(c, dst_id, item, top + i, dst)
    top = c.execute(
        f"SELECT COALESCE(MAX(position), -1) + 1 FROM {dst.patches} WHERE {dst.col}=?", (dst_id,)
    ).fetchone()[0]
    for i, p in enumerate(patches):
        blocks = [
            EffectBlockIn(slot=b["slot"], block_type=b["block_type"], model=b["model"], enabled=b["enabled"],
                          params=[Knob(**k) for k in b["params"]], scene_overrides=b["scene_overrides"])
            for b in p["blocks"]
        ]
        item = PatchIn(gear_id=p["gear_id"], gear_name=p["gear_name"], patch_ref=p["patch_ref"],
                       patch_name=p["patch_name"], scenes=p["scenes"], midi=p["midi"], note=p["note"],
                       blocks=blocks)
        insert_patch(c, dst_id, item, top + i, dst)


def save_song_as_preset(c, song_row, body: SaveAsPresetIn, user_id: int | None) -> dict[str, Any]:
    stamp = now_iso()
    song_id = song_row["id"]
    preset_id = c.execute(
        """INSERT INTO presets(name,artist,amp_id,amp_name,notes,created_by,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        (body.name.strip(), song_row["artist"], song_row["amp_id"], song_row["amp_name"], "", user_id, stamp, stamp),
    ).lastrowid
    copy_chain(c, song_id, SONG, preset_id, PRESET)
    if body.use_in_song:
        # the song now points at the preset instead of keeping its own copy
        c.execute("DELETE FROM song_gear_settings WHERE song_id=?", (song_id,))
        c.execute("DELETE FROM song_device_patches WHERE song_id=?", (song_id,))
        top = c.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM song_presets WHERE song_id=?", (song_id,)
        ).fetchone()[0]
        c.execute(
            "INSERT INTO song_presets(song_id,preset_id,position,label,note) VALUES(?,?,?,?,?)",
            (song_id, preset_id, top, "", ""),
        )
        touch_song(c, song_id)
    return preset_dict(c, get_preset_row(c, preset_id))


def unlink_preset_to_copy(c, song_id: int, link_id: int) -> dict[str, Any]:
    get_song_row(c, song_id)
    link = c.execute("SELECT * FROM song_presets WHERE id=? AND song_id=?", (link_id, song_id)).fetchone()
    if not link:
        raise HTTPException(404, "This song doesn't use that preset")
    copy_chain(c, link["preset_id"], PRESET, song_id, SONG)
    c.execute("DELETE FROM song_presets WHERE id=?", (link_id,))
    touch_song(c, song_id)
    return song_dict(c, get_song_row(c, song_id))


def artist_groups(c) -> list[dict[str, Any]]:
    """Songs and presets grouped by artist, ignoring case and stray spaces."""
    groups: dict[str, dict[str, Any]] = {}

    def group(name: str) -> dict[str, Any]:
        tidy = " ".join(name.split())
        key = tidy.lower()
        if key not in groups:
            groups[key] = {"artist": tidy, "songs": [], "presets": []}
        elif groups[key]["artist"].islower() and not tidy.islower():
            groups[key]["artist"] = tidy  # prefer "AC/DC" over "ac/dc" for the heading
        return groups[key]

    for s in list_song_rows(c):
        group(s["artist"])["songs"].append(s)
    for p in list_preset_rows(c):
        group(p["artist"])["presets"].append(p)
    named = sorted((g for k, g in groups.items() if k), key=lambda g: g["artist"].lower())
    for g in named:
        g["song_count"], g["preset_count"] = len(g["songs"]), len(g["presets"])
    if "" in groups:
        blank = groups[""]
        blank["song_count"], blank["preset_count"] = len(blank["songs"]), len(blank["presets"])
        named.append(blank)
    return named


def register_preset_routes(prefix: str, auth, v1: bool) -> None:
    """Preset and artist routes for the web app (session cookie) and the token API (/api/v1)."""
    extra: dict[str, Any] = {"tags": ["v1"]} if v1 else {"include_in_schema": False}
    tag = "v1_" if v1 else ""

    def route(method: str, path: str, summary: str, **kw):
        return getattr(app, method)(prefix + path, summary=summary, name=f"{tag}{method}_{path}", **extra, **kw)

    @route("get", "/presets", "List presets (filter by name/artist with q, or an exact artist)")
    def _list(request: Request, q: str | None = None, artist: str | None = None):
        auth(request)
        with db() as c:
            return list_preset_rows(c, q, artist)

    @route("post", "/presets", "Add a preset: a named tone with its rig settings and patches", status_code=201)
    def _add(body: PresetIn, request: Request):
        user = auth(request)
        with db() as c:
            return preset_dict(c, get_preset_row(c, create_preset(c, body, user["id"])))

    @route("get", "/presets/{preset_id}", "Get one preset with its chain and the songs that use it")
    def _get(preset_id: int, request: Request):
        auth(request)
        with db() as c:
            return preset_dict(c, get_preset_row(c, preset_id))

    @route("patch", "/presets/{preset_id}",
           "Update a preset; every song using it sees the change. Sending rig or patches replaces that list")
    def _patch(preset_id: int, body: PresetPatch, request: Request):
        auth(request)
        with db() as c:
            update_preset(c, get_preset_row(c, preset_id), body)
            return preset_dict(c, get_preset_row(c, preset_id))

    @route("delete", "/presets/{preset_id}", "Delete a preset (songs using it just lose the link)")
    def _delete(preset_id: int, request: Request):
        auth(request)
        with db() as c:
            get_preset_row(c, preset_id)
            c.execute("DELETE FROM presets WHERE id=?", (preset_id,))
            return {"ok": True}

    @route("post", "/songs/{song_id}/save-as-preset",
           "Save a song's chain as a new preset; use_in_song swaps the song over to the preset", status_code=201)
    def _save_as(song_id: int, body: SaveAsPresetIn, request: Request):
        user = auth(request)
        with db() as c:
            return save_song_as_preset(c, get_song_row(c, song_id), body, user["id"])

    @route("post", "/songs/{song_id}/presets/{link_id}/copy",
           "Stop using a preset in this song and keep an editable copy of its chain instead")
    def _copy(song_id: int, link_id: int, request: Request):
        auth(request)
        with db() as c:
            return unlink_preset_to_copy(c, song_id, link_id)

    @route("get", "/artists", "Songs and presets grouped by artist")
    def _artists(request: Request):
        auth(request)
        with db() as c:
            return artist_groups(c)


def session_user(request: Request) -> sqlite3.Row:
    return current_user(request)


def token_user(request: Request) -> sqlite3.Row:
    return token_auth(request)[1]


def register_song_routes(prefix: str, auth, v1: bool) -> None:
    """The same song routes for the web app (session cookie) and the token API (/api/v1)."""
    extra: dict[str, Any] = {"tags": ["v1"]} if v1 else {"include_in_schema": False}
    tag = "v1_" if v1 else ""

    def route(method: str, path: str, summary: str, **kw):
        return getattr(app, method)(prefix + path, summary=summary, name=f"{tag}{method}_{path}", **extra, **kw)

    @route("get", "/songs", "List songs (filter by title/artist with q, an exact artist, or gear_id)")
    def _list(request: Request, q: str | None = None, gear_id: int | None = None, artist: str | None = None):
        auth(request)
        with db() as c:
            return list_song_rows(c, q, gear_id, artist)

    @route("post", "/songs", "Add a song, optionally with its rig settings and device patches", status_code=201)
    def _add(body: SongIn, request: Request):
        user = auth(request)
        with db() as c:
            return song_dict(c, get_song_row(c, create_song(c, body, user["id"])))

    @route("get", "/songs/{song_id}", "Get one song with its full rig, patches and photos")
    def _get(song_id: int, request: Request):
        auth(request)
        with db() as c:
            return song_dict(c, get_song_row(c, song_id))

    @route("patch", "/songs/{song_id}",
           "Update a song. Sending rig or patches replaces that whole list; leave them out to keep them")
    def _patch(song_id: int, body: SongPatch, request: Request):
        auth(request)
        with db() as c:
            update_song(c, get_song_row(c, song_id), body)
            return song_dict(c, get_song_row(c, song_id))

    @route("delete", "/songs/{song_id}", "Delete a song (the gear stays)")
    def _delete(song_id: int, request: Request):
        auth(request)
        with db() as c:
            return remove_song(c, song_id)

    @route("post", "/songs/{song_id}/rig", "Add one piece of gear with its settings to a song's rig",
           status_code=201)
    def _rig_add(song_id: int, body: RigSettingIn, request: Request):
        auth(request)
        with db() as c:
            return add_rig_setting(c, song_id, body)

    @route("patch", "/songs/{song_id}/rig/{setting_id}", "Change one rig entry's knobs, on/off state, order or note")
    def _rig_edit(song_id: int, setting_id: int, body: RigSettingPatch, request: Request):
        auth(request)
        with db() as c:
            return edit_rig_setting(c, song_id, setting_id, body)

    @route("delete", "/songs/{song_id}/rig/{setting_id}", "Remove one piece of gear from a song's rig")
    def _rig_delete(song_id: int, setting_id: int, request: Request):
        auth(request)
        with db() as c:
            return delete_rig_setting(c, song_id, setting_id)

    @route("post", "/songs/{song_id}/patches", "Add a modeler patch pointer to a song", status_code=201)
    def _patch_add(song_id: int, body: PatchIn, request: Request):
        auth(request)
        with db() as c:
            return add_patch(c, song_id, body)

    @route("patch", "/songs/{song_id}/patches/{patch_id}",
           "Change a patch pointer (sending blocks replaces its effect blocks)")
    def _patch_edit(song_id: int, patch_id: int, body: PatchPatch, request: Request):
        auth(request)
        with db() as c:
            return edit_patch(c, song_id, patch_id, body)

    @route("delete", "/songs/{song_id}/patches/{patch_id}", "Remove a patch pointer from a song")
    def _patch_delete(song_id: int, patch_id: int, request: Request):
        auth(request)
        with db() as c:
            return delete_patch(c, song_id, patch_id)

    @route("post", "/songs/{song_id}/photos", "Attach a photo to a song (multipart field \"photo\")",
           status_code=201)
    async def _photo_add(song_id: int, request: Request, photo: UploadFile = File(...)):
        auth(request)
        with db() as c:
            return await attach_song_photo(c, song_id, photo)

    @route("delete", "/song-photos/{photo_id}", "Delete a song photo")
    def _photo_delete(photo_id: int, request: Request):
        auth(request)
        with db() as c:
            return remove_song_photo(c, photo_id)

    @route("post", "/song-photos/{photo_id}/cover", "Make a song photo the cover")
    def _photo_cover(photo_id: int, request: Request):
        auth(request)
        with db() as c:
            return set_song_cover(c, photo_id)


register_song_routes("/api", session_user, v1=False)
register_song_routes("/api/v1", token_user, v1=True)
register_share_routes("/api", session_user, v1=False)
register_share_routes("/api/v1", token_user, v1=True)
register_preset_routes("/api", session_user, v1=False)
register_preset_routes("/api/v1", token_user, v1=True)


@app.get("/api/song-options")
def song_options(request: Request):
    """Tuning suggestions for the song editor."""
    current_user(request)
    return {"tunings": TUNING_SUGGESTIONS}


# ---------------------------------------------------------------- backup / export


@app.get("/api/export")
def export_data(request: Request):
    """The whole collection as one JSON download. Photo files stay in the data folder;
    the export carries their stored names and urls."""
    current_user(request)
    with db() as c:
        payload = {
            "app": "Gearsmith",
            "version": APP_VERSION,
            "exported_at": now_iso(),
            "gear": [gear_dict(c, r) for r in c.execute("SELECT * FROM gear ORDER BY id")],
            "restrings": [restring_dict(c, r) for r in c.execute("SELECT * FROM restrings ORDER BY id")],
            "maintenance": [maintenance_dict(c, r) for r in c.execute("SELECT * FROM maintenance ORDER BY id")],
            "sets": [set_dict(c, r) for r in c.execute("SELECT * FROM sets ORDER BY id")],
            "songs": [song_dict(c, r) for r in c.execute("SELECT * FROM songs ORDER BY id")],
            "presets": [preset_dict(c, r) for r in c.execute("SELECT * FROM presets ORDER BY id")],
        }
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="gearsmith-export-{today().isoformat()}.json"'},
    )


# ---------------------------------------------------------------- settings

SETTINGS_DEFAULTS = {
    "app_name": "Gearsmith",
    **{f"feature_{name}": "1" for name in FEATURES},
}


def read_settings(c) -> dict[str, Any]:
    out = {k: get_setting(c, k, v) for k, v in SETTINGS_DEFAULTS.items()}
    for name in FEATURES:
        out[f"feature_{name}"] = out[f"feature_{name}"] == "1"
    return out


class SettingsIn(BaseModel):
    app_name: str | None = Field(default=None, max_length=60)
    feature_guitars: bool | None = None
    feature_amps: bool | None = None
    feature_pedals: bool | None = None
    feature_picks: bool | None = None
    feature_strings: bool | None = None
    feature_sets: bool | None = None
    feature_maintenance: bool | None = None
    feature_songs: bool | None = None
    feature_want: bool | None = None
    feature_sold: bool | None = None
    feature_tuner: bool | None = None


@app.get("/api/settings")
def get_settings(request: Request):
    current_user(request)
    with db() as c:
        return read_settings(c)


@app.put("/api/settings")
def update_settings(body: SettingsIn, request: Request):
    current_user(request, True)
    with db() as c:
        if body.app_name is not None:
            if not body.app_name.strip():
                raise HTTPException(400, "Name is required")
            set_setting(c, "app_name", body.app_name.strip())
        for name in FEATURES:
            value = getattr(body, f"feature_{name}")
            if value is not None:
                set_setting(c, f"feature_{name}", "1" if value else "0")
        return read_settings(c)


# ---------------------------------------------------------------- users


class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserUpdate(BaseModel):
    is_admin: bool | None = None
    active: bool | None = None
    password: str | None = None


@app.get("/api/users")
def list_users(request: Request):
    current_user(request, True)
    with db() as c:
        return [public_user(r) for r in c.execute("SELECT * FROM users ORDER BY username")]


@app.post("/api/users", status_code=201)
def create_user(body: UserCreate, request: Request):
    current_user(request, True)
    username = clean_username(body.username)
    pw, salt = password_record(body.password)
    with db() as c:
        try:
            uid = c.execute(
                "INSERT INTO users(username,password_hash,salt,is_admin,active,created_at) VALUES(?,?,?,?,1,?)",
                (username, pw, salt, int(body.is_admin), now_iso()),
            ).lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That username is taken")
        return public_user(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


@app.put("/api/users/{item_id}")
def update_user(item_id: int, body: UserUpdate, request: Request):
    me_row = current_user(request, True)
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        if item_id == me_row["id"] and (body.is_admin is False or body.active is False):
            raise HTTPException(400, "You can't demote or deactivate yourself")
        if body.is_admin is not None:
            c.execute("UPDATE users SET is_admin=? WHERE id=?", (int(body.is_admin), item_id))
        if body.active is not None:
            c.execute("UPDATE users SET active=? WHERE id=?", (int(body.active), item_id))
            if not body.active:
                c.execute("DELETE FROM sessions WHERE user_id=?", (item_id,))
        if body.password is not None:
            pw, salt = password_record(body.password)
            c.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?", (pw, salt, item_id))
            c.execute("DELETE FROM sessions WHERE user_id=?", (item_id,))
        return public_user(c.execute("SELECT * FROM users WHERE id=?", (item_id,)).fetchone())


# ---------------------------------------------------------------- API tokens


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


def token_dict(row, raw: str | None = None) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "name": row["name"],
        "prefix": row["prefix"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "created_by": None,
    }
    if raw:
        out["token"] = raw
    return out


@app.get("/api/tokens")
def list_tokens(request: Request):
    user = current_user(request)
    with db() as c:
        rows = c.execute(
            "SELECT * FROM api_tokens WHERE revoked=0 ORDER BY id"
        ).fetchall()
        out = []
        for r in rows:
            d = token_dict(r)
            d["created_by"] = display_user(c, r["created_by"])
            if not user["is_admin"] and r["created_by"] != user["id"]:
                continue
            out.append(d)
        return out


@app.post("/api/tokens", status_code=201)
def create_token(body: TokenIn, request: Request):
    user = current_user(request)
    raw = "gs_" + secrets.token_urlsafe(24)
    with db() as c:
        row_id = c.execute(
            "INSERT INTO api_tokens(name,token_hash,prefix,created_by,created_at) VALUES(?,?,?,?,?)",
            (body.name.strip(), hashlib.sha256(raw.encode()).hexdigest(), raw[:8], user["id"], now_iso()),
        ).lastrowid
        row = c.execute("SELECT * FROM api_tokens WHERE id=?", (row_id,)).fetchone()
        d = token_dict(row, raw)
        d["created_by"] = user["username"]
        return d


@app.delete("/api/tokens/{item_id}")
def revoke_token(item_id: int, request: Request):
    user = current_user(request)
    with db() as c:
        row = c.execute("SELECT * FROM api_tokens WHERE id=? AND revoked=0", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Token not found")
        if not user["is_admin"] and row["created_by"] != user["id"]:
            raise HTTPException(403, "You can only revoke your own tokens")
        c.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (item_id,))
        return {"ok": True}


# ---------------------------------------------------------------- token API (v1)
# Everything below /api/v1 authenticates with a Bearer token from Settings > API tokens
# and acts as the user who created the token. Interactive docs live at /api/docs.


@app.get("/api/v1/gear", tags=["v1"],
         summary="List gear, optionally filtered by type (guitar, amp, pedal, pick, strings), "
                 "lifecycle (owned, want, sold) and a search term q")
def v1_list_gear(request: Request, type: str | None = None, lifecycle: str | None = None, q: str | None = None):
    token_auth(request)
    with db() as c:
        return list_gear_rows(c, type, lifecycle, q)


@app.post("/api/v1/gear", tags=["v1"], status_code=201, summary="Add a piece of gear")
def v1_add_gear(body: GearIn, request: Request):
    _, user = token_auth(request)
    with db() as c:
        gear_id = create_gear(c, body, user["id"])
        return gear_dict(c, get_gear_row(c, gear_id))


@app.get("/api/v1/gear/{gear_id}", tags=["v1"], summary="Get one piece of gear")
def v1_get_gear(gear_id: int, request: Request):
    token_auth(request)
    with db() as c:
        return gear_dict(c, get_gear_row(c, gear_id))


@app.patch("/api/v1/gear/{gear_id}", tags=["v1"], summary="Update some fields of a piece of gear")
def v1_patch_gear(gear_id: int, body: GearPatch, request: Request):
    token_auth(request)
    with db() as c:
        patch_gear(c, get_gear_row(c, gear_id), body)
        return gear_dict(c, get_gear_row(c, gear_id))


@app.delete("/api/v1/gear/{gear_id}", tags=["v1"], summary="Delete a piece of gear")
def v1_delete_gear(gear_id: int, request: Request):
    token_auth(request)
    with db() as c:
        return remove_gear(c, gear_id)


@app.get("/api/v1/gear/{gear_id}/restrings", tags=["v1"], summary="List a guitar's restring history")
def v1_list_restrings(gear_id: int, request: Request):
    token_auth(request)
    with db() as c:
        get_gear_row(c, gear_id)
        rows = c.execute(
            "SELECT * FROM restrings WHERE gear_id=? ORDER BY date DESC, id DESC", (gear_id,)
        ).fetchall()
        return [restring_dict(c, r) for r in rows]


@app.post("/api/v1/gear/{gear_id}/restrings", tags=["v1"], status_code=201,
          summary="Log a restring for a guitar (resets its string-age counter)")
def v1_log_restring(gear_id: int, body: RestringIn, request: Request):
    _, user = token_auth(request)
    with db() as c:
        rid = log_restring(c, gear_id, body, user["id"], via="api")
        row = c.execute("SELECT * FROM restrings WHERE id=?", (rid,)).fetchone()
        return restring_dict(c, row)


@app.get("/api/v1/gear/{gear_id}/maintenance", tags=["v1"], summary="List an item's maintenance log")
def v1_list_maintenance(gear_id: int, request: Request):
    token_auth(request)
    with db() as c:
        return maintenance_list(c, gear_id)


@app.post("/api/v1/gear/{gear_id}/maintenance", tags=["v1"], status_code=201,
          summary="Log maintenance for an item (setup, tubes, fret work, repair, other)")
def v1_add_maintenance(gear_id: int, body: MaintenanceIn, request: Request):
    _, user = token_auth(request)
    with db() as c:
        entry_id = log_maintenance(c, gear_id, body, user["id"])
        return maintenance_dict(c, get_maintenance_row(c, entry_id))


@app.patch("/api/v1/maintenance/{entry_id}", tags=["v1"], summary="Edit a maintenance entry")
def v1_edit_maintenance(entry_id: int, body: MaintenancePatch, request: Request):
    token_auth(request)
    with db() as c:
        return edit_maintenance(c, entry_id, body)


@app.delete("/api/v1/maintenance/{entry_id}", tags=["v1"], summary="Delete a maintenance entry")
def v1_delete_maintenance(entry_id: int, request: Request):
    token_auth(request)
    with db() as c:
        return delete_maintenance_entry(c, entry_id)


@app.get("/api/v1/due", tags=["v1"], summary="Guitars whose strings are overdue or due within N days")
def v1_due(request: Request, days: int = 7):
    token_auth(request)
    if days < 0 or days > 90:
        raise HTTPException(400, "Days must be 0-90")
    with db() as c:
        return due_items(c, days)


@app.get("/api/v1/sets", tags=["v1"], summary="List gear sets (rigs and boards)")
def v1_list_sets(request: Request):
    token_auth(request)
    with db() as c:
        rows = c.execute("SELECT * FROM sets ORDER BY name COLLATE NOCASE").fetchall()
        return [set_dict(c, r) for r in rows]


@app.post("/api/v1/sets", tags=["v1"], status_code=201, summary="Create a gear set")
def v1_add_set(body: SetIn, request: Request):
    token_auth(request)
    stamp = now_iso()
    with db() as c:
        try:
            set_id = c.execute(
                "INSERT INTO sets(name,notes,created_at,updated_at) VALUES(?,?,?,?)",
                (body.name.strip(), body.notes.strip(), stamp, stamp),
            ).lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A set with that name already exists")
        if body.item_ids:
            replace_set_items(c, set_id, body.item_ids)
        return set_dict(c, get_set_row(c, set_id))


@app.get("/api/v1/sets/{set_id}", tags=["v1"], summary="Get one gear set")
def v1_get_set(set_id: int, request: Request):
    token_auth(request)
    with db() as c:
        return set_dict(c, get_set_row(c, set_id))


@app.patch("/api/v1/sets/{set_id}", tags=["v1"], summary="Rename a set, edit its notes, or replace its items")
def v1_patch_set(set_id: int, body: SetPatch, request: Request):
    token_auth(request)
    with db() as c:
        get_set_row(c, set_id)
        data = body.model_dump(exclude_unset=True)
        item_ids = data.pop("item_ids", None)
        if data:
            if "name" in data:
                data["name"] = data["name"].strip()
            if "notes" in data and data["notes"] is not None:
                data["notes"] = data["notes"].strip()
            data["updated_at"] = now_iso()
            cols = ", ".join(f"{k}=?" for k in data)
            try:
                c.execute(f"UPDATE sets SET {cols} WHERE id=?", (*data.values(), set_id))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "A set with that name already exists")
        if item_ids is not None:
            replace_set_items(c, set_id, item_ids)
        return set_dict(c, get_set_row(c, set_id))


@app.delete("/api/v1/sets/{set_id}", tags=["v1"], summary="Delete a gear set (the gear itself stays)")
def v1_delete_set(set_id: int, request: Request):
    token_auth(request)
    with db() as c:
        get_set_row(c, set_id)
        c.execute("DELETE FROM sets WHERE id=?", (set_id,))
        return {"ok": True}


@app.post("/api/v1/gear/{gear_id}/photos", tags=["v1"], status_code=201,
          summary="Attach a photo to a piece of gear")
async def v1_add_photo(gear_id: int, request: Request, photo: UploadFile = File(...)):
    token_auth(request)
    with db() as c:
        return await attach_photo(c, gear_id, photo)


@app.delete("/api/v1/photos/{photo_id}", tags=["v1"],
            summary="Delete a photo (photo ids are in the gear item's photos list)")
def v1_delete_photo(photo_id: int, request: Request):
    token_auth(request)
    with db() as c:
        return remove_photo(c, photo_id)


@app.post("/api/v1/photos/{photo_id}/cover", tags=["v1"],
          summary="Make a photo the cover of its gear item")
def v1_make_cover(photo_id: int, request: Request):
    token_auth(request)
    with db() as c:
        return set_cover(c, photo_id)


def v1_openapi() -> dict[str, Any]:
    routes = [r for r in app.routes if getattr(r, "path", "").startswith("/api/v1/")]
    return get_openapi(
        title="Gearsmith API",
        version=APP_VERSION,
        description=(
            "Token API for Gearsmith. Create a token in Settings > API tokens, then send it as "
            "`Authorization: Bearer <token>`. Writes act as the user who created the token."
        ),
        routes=routes,
    )


@app.get("/api/v1/openapi.json", include_in_schema=False)
def v1_openapi_json():
    return JSONResponse(v1_openapi())


@app.get("/api/docs", include_in_schema=False)
def v1_docs():
    return get_swagger_ui_html(
        openapi_url="/api/v1/openapi.json",
        title="Gearsmith API",
    )


# ---------------------------------------------------------------- notifications (Apprise)

NOTIFY_DEFAULTS = {
    "notify_urls": "",
    "notify_mode": "digest",
    "notify_hour": "8",
    "quiet_start": "21",
    "quiet_end": "8",
    "overdue_repeat_days": "2",
    "public_url": "",
}


def send_notification(urls: str, title: str, body: str) -> tuple[bool, str]:
    targets = urls.split()
    if not targets:
        return False, "no notification URLs"
    try:
        import apprise
    except ImportError:
        return False, "apprise is not installed"
    ap = apprise.Apprise()
    for url in targets:
        ap.add(url)
    if ap.notify(title=title, body=body):
        return True, ""
    return False, "delivery failed; check the URL and its service"


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def notification_items(c, now: datetime, state_key: str = "notify_state") -> list[dict[str, Any]]:
    """Guitars with dead strings that should be announced now, honoring the repeat setting."""
    state = json.loads(get_setting(c, state_key, "{}") or "{}")
    try:
        repeat = int(get_setting(c, "overdue_repeat_days", NOTIFY_DEFAULTS["overdue_repeat_days"]))
    except ValueError:
        repeat = 2
    on = now.date()
    out = []
    for item in due_items(c, 0, on):
        if item["state"] != "overdue":
            continue
        prev = state.get(str(item["gear_id"]))
        if not prev or prev.get("due") != item["due_date"]:
            out.append(item)
        elif repeat > 0:
            sent = iso_date(prev.get("sent"))
            if sent and (on - sent).days >= repeat:
                out.append(item)
    return out


def describe(item: dict[str, Any], base: str) -> str:
    line = f"{item['name']}: strings are {-item['days_until_due']} day{'s' if item['days_until_due'] != -1 else ''} overdue"
    line += f" (last changed {item['last_date']}, every {item['interval_days']} days)"
    if base:
        line += f"\n{base.rstrip('/')}/#/gear/{item['gear_id']}"
    return line


def mark_announced(c, key: str, items: list[dict[str, Any]], now: datetime) -> None:
    state = json.loads(get_setting(c, key, "{}") or "{}")
    for item in items:
        state[str(item["gear_id"])] = {"due": item["due_date"], "sent": now.date().isoformat()}
    live = {str(r[0]) for r in c.execute("SELECT id FROM gear WHERE type='guitar' AND lifecycle='owned'")}
    state = {k: v for k, v in state.items() if k in live}
    set_setting(c, key, json.dumps(state))


def run_notification_check(now: datetime | None = None, force: bool = False) -> int:
    """Send overdue-restring alerts through Apprise. Returns how many items were announced."""
    now = now or datetime.now().astimezone()
    with db() as c:
        if not feature_on(c, "maintenance"):
            return 0
        cfg = {k: get_setting(c, k, v) for k, v in NOTIFY_DEFAULTS.items()}
        if not cfg["notify_urls"].strip():
            return 0
        if not force:
            if in_quiet_hours(now.hour, int(cfg["quiet_start"]), int(cfg["quiet_end"])):
                return 0
            if now.hour < int(cfg["notify_hour"]):
                return 0
        app_name = get_setting(c, "app_name", "Gearsmith")
        base = cfg["public_url"].strip()
        items = notification_items(c, now)
        if not items:
            return 0
        if cfg["notify_mode"] == "each":
            for item in items:
                ok, detail = send_notification(cfg["notify_urls"], f"{app_name}: {item['name']}", describe(item, base))
                if not ok:
                    raise RuntimeError(detail)
        else:
            body = "\n".join(describe(i, base) for i in items)
            n = len(items)
            ok, detail = send_notification(
                cfg["notify_urls"], f"{app_name}: {n} guitar{'s' if n != 1 else ''} need new strings", body
            )
            if not ok:
                raise RuntimeError(detail)
        mark_announced(c, "notify_state", items, now)
        return len(items)


def notification_worker() -> None:
    while True:
        try:
            run_notification_check()
        except Exception as exc:
            print(f"notification check failed: {exc}")
        time.sleep(600)


class NotificationSettingsIn(BaseModel):
    notify_urls: str = Field(default="", max_length=2000)
    notify_mode: Literal["digest", "each"] = "digest"
    notify_hour: int = Field(default=8, ge=0, le=23)
    quiet_start: int = Field(default=21, ge=0, le=23)
    quiet_end: int = Field(default=8, ge=0, le=23)
    overdue_repeat_days: int = Field(default=2, ge=0, le=30)
    public_url: str = Field(default="", max_length=300)

    @field_validator("public_url")
    @classmethod
    def _url(cls, v):
        v = (v or "").strip()
        if v and not re.match(r"^https?://", v, re.I):
            raise ValueError("App address must start with http:// or https://")
        return v


def notify_settings(c) -> dict[str, Any]:
    cfg = {k: get_setting(c, k, v) for k, v in NOTIFY_DEFAULTS.items()}
    for k in ("notify_hour", "quiet_start", "quiet_end", "overdue_repeat_days"):
        cfg[k] = int(cfg[k])
    return cfg


@app.get("/api/notifications")
def get_notifications(request: Request):
    current_user(request, True)
    with db() as c:
        return notify_settings(c)


@app.put("/api/notifications")
def put_notifications(body: NotificationSettingsIn, request: Request):
    current_user(request, True)
    urls = body.notify_urls.strip()
    if any("://" not in u for u in urls.split()):
        raise HTTPException(400, "Each notification URL needs a scheme, like ntfy:// or pover://")
    with db() as c:
        data = body.model_dump()
        data["notify_urls"] = urls
        for k, v in data.items():
            set_setting(c, k, str(v))
        return notify_settings(c)


@app.post("/api/notifications/test")
def test_notification(request: Request):
    current_user(request, True)
    with db() as c:
        cfg = notify_settings(c)
        app_name = get_setting(c, "app_name", "Gearsmith")
    ok, detail = send_notification(cfg["notify_urls"], f"{app_name}: test", "Notifications are working.")
    if not ok:
        raise HTTPException(400, f"Test notification failed: {detail}")
    return {"ok": True}


# ---------------------------------------------------------------- app shell


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

"""Gearsmith: a small self-hosted gear tracker for guitarists."""

from __future__ import annotations

import hashlib
import hmac
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
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
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

APP_VERSION = "0.1.0"

GEAR_TYPES = ("guitar", "amp", "pedal", "pick")
GEAR_TYPE_LABELS = {"guitar": "Guitars", "amp": "Amps", "pedal": "Pedals", "pick": "Picks"}
GEAR_STATUS = ("", "home", "luthier", "lent")
GEAR_STATUS_LABELS = {"": "Unspecified", "home": "Home", "luthier": "At the luthier", "lent": "Lent out"}

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
}

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
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=()"
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
          type TEXT NOT NULL CHECK (type IN ('guitar','amp','pedal','pick')),
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
        CREATE INDEX IF NOT EXISTS idx_gear_type ON gear(type);
        CREATE INDEX IF NOT EXISTS idx_restrings_gear ON restrings(gear_id, date);
        CREATE INDEX IF NOT EXISTS idx_photos_gear ON gear_photos(gear_id);
        """
        )
        if fresh:
            seed_example(c)


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

    # One guitar fresh, one overdue, so both chip states are visible.
    old = (today() - timedelta(days=100)).isoformat()
    recent = (today() - timedelta(days=20)).isoformat()
    c.execute(
        "INSERT INTO restrings(gear_id,brand,gauge,date,note,created_at) VALUES(?,?,?,?,?,?)",
        (starling, "Demo Strings", "10-46", old, "Example restring.", stamp),
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
FEATURES = ("guitars", "amps", "pedals", "picks", "sets", "maintenance")
FEATURE_LABELS = {
    "guitars": "Guitars section",
    "amps": "Amps section",
    "pedals": "Pedals section",
    "picks": "Picks section",
    "sets": "Sets (rigs and boards)",
    "maintenance": "Maintenance (restring tracking)",
}
FEATURE_FOR_TYPE = {"guitar": "guitars", "amp": "amps", "pedal": "pedals", "pick": "picks"}


def feature_on(c, name: str) -> bool:
    return get_setting(c, f"feature_{name}", "1") == "1"


# ---------------------------------------------------------------- gear


def clean_specs(type_: str, specs: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only the fields that belong to this gear type, coerced to text or numbers."""
    out: dict[str, Any] = {}
    allowed = {key: (label, numeric) for key, label, numeric in SPEC_FIELDS[type_]}
    for key, value in (specs or {}).items():
        if key not in allowed or value is None:
            continue
        label, numeric = allowed[key]
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
        "SELECT date, brand, gauge FROM restrings WHERE gear_id=? ORDER BY date DESC, id DESC LIMIT 1",
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
        "due_date": due.isoformat(),
        "days_until_due": (due - on).days,
    }


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


def gear_dict(c, row, on: date | None = None) -> dict[str, Any]:
    specs = json.loads(row["specs"] or "{}")
    photos = photo_list(c, row["id"])
    return {
        "id": row["id"],
        "type": row["type"],
        "type_label": GEAR_TYPE_LABELS[row["type"]],
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
        "strings": strings_info(c, row, on),
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
    type: Literal["guitar", "amp", "pedal", "pick"]
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

    @field_validator("purchase_date")
    @classmethod
    def _date(cls, v):
        return check_date(v)


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

    @field_validator("purchase_date")
    @classmethod
    def _date(cls, v):
        return check_date(v)


def create_gear(c, body: GearIn, user_id: int | None) -> int:
    if body.type != "guitar" and body.restring_interval_days is not None:
        raise HTTPException(400, "Only guitars have a restring interval")
    stamp = now_iso()
    return c.execute(
        """INSERT INTO gear(type,name,make,model,year,serial,specs,status,purchase_date,
           purchase_price,notes,restring_interval_days,created_by,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            body.type, body.name.strip(), body.make.strip(), body.model.strip(), body.year,
            body.serial.strip(), json.dumps(clean_specs(body.type, body.specs)), body.status,
            body.purchase_date, body.purchase_price, body.notes.strip(), body.restring_interval_days,
            user_id, stamp, stamp,
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
    if "name" in data:
        data["name"] = data["name"].strip()
    for key in ("make", "model", "serial", "notes"):
        if key in data and data[key] is not None:
            data[key] = data[key].strip()
    data["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in data)
    c.execute(f"UPDATE gear SET {cols} WHERE id=?", (*data.values(), row["id"]))


@app.get("/api/gear")
def list_gear(request: Request, type: str | None = None):
    current_user(request)
    with db() as c:
        sql = "SELECT * FROM gear"
        params: tuple = ()
        if type:
            if type not in GEAR_TYPES:
                raise HTTPException(400, "Unknown gear type")
            sql += " WHERE type=?"
            params = (type,)
        sql += " ORDER BY type, name COLLATE NOCASE"
        return [gear_dict(c, r) for r in c.execute(sql, params)]


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
        patch = GearPatch(**body.model_dump(exclude={"type"}))
        patch_gear(c, row, patch)
        return gear_dict(c, get_gear_row(c, gear_id))


@app.patch("/api/gear/{gear_id}")
def update_gear(gear_id: int, body: GearPatch, request: Request):
    current_user(request)
    with db() as c:
        patch_gear(c, get_gear_row(c, gear_id), body)
        return gear_dict(c, get_gear_row(c, gear_id))


@app.delete("/api/gear/{gear_id}")
def delete_gear(gear_id: int, request: Request):
    current_user(request)
    with db() as c:
        row = get_gear_row(c, gear_id)
        for photo in photo_list(c, gear_id):
            unlink_photo(photo["url"].rsplit("/", 1)[-1])
        c.execute("DELETE FROM gear WHERE id=?", (row["id"],))
        return {"ok": True}


# ---------------------------------------------------------------- restring log


class RestringIn(BaseModel):
    brand: str = Field(default="", max_length=80)
    gauge: str = Field(default="", max_length=40)
    date: str | None = None
    note: str = Field(default="", max_length=1000)

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
    rid = c.execute(
        """INSERT INTO restrings(gear_id,brand,gauge,date,note,user_id,via,created_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        (gear_id, body.brand.strip(), body.gauge.strip(), when.isoformat(), body.note.strip(),
         user_id, via, now_iso()),
    ).lastrowid
    # A fresh set of strings usually means the current gauge changed too.
    if body.gauge.strip():
        specs = json.loads(row["specs"] or "{}")
        specs["string_gauge"] = body.gauge.strip()
        c.execute("UPDATE gear SET specs=? WHERE id=?", (json.dumps(specs), gear_id))
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


# ---------------------------------------------------------------- due list


def due_items(c, horizon: int = 7, on: date | None = None) -> list[dict[str, Any]]:
    """Guitars whose strings are overdue or coming due within `horizon` days."""
    on = on or today()
    out = []
    rows = c.execute("SELECT * FROM gear WHERE type='guitar' ORDER BY name COLLATE NOCASE").fetchall()
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
    feature_sets: bool | None = None
    feature_maintenance: bool | None = None


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


@app.get("/api/v1/gear", tags=["v1"], summary="List gear, optionally filtered by type")
def v1_list_gear(request: Request, type: str | None = None):
    token_auth(request)
    with db() as c:
        sql = "SELECT * FROM gear"
        params: tuple = ()
        if type:
            if type not in GEAR_TYPES:
                raise HTTPException(400, "Unknown gear type")
            sql += " WHERE type=?"
            params = (type,)
        sql += " ORDER BY type, name COLLATE NOCASE"
        return [gear_dict(c, r) for r in c.execute(sql, params)]


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
        row = get_gear_row(c, gear_id)
        for photo in photo_list(c, gear_id):
            unlink_photo(photo["url"].rsplit("/", 1)[-1])
        c.execute("DELETE FROM gear WHERE id=?", (row["id"],))
        return {"ok": True}


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
    live = {str(r[0]) for r in c.execute("SELECT id FROM gear WHERE type='guitar'")}
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

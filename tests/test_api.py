import os
from datetime import date, timedelta

os.environ["GEARSMITH_DATA_DIR"] = "/tmp/gearsmith-pytest-data"
os.environ["GEARSMITH_NOTIFY_WORKER"] = "false"
from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def fresh(tmp_path):
    main.DB_PATH = tmp_path / "gearsmith.db"
    main.PHOTOS_DIR = tmp_path / "photos"
    main.PHOTOS_DIR.mkdir()
    main._login_failures.clear()
    main._api_failures.clear()
    main._api_calls.clear()
    main.init_db()


def days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def setup_admin(c):
    assert c.post("/api/setup", json={"username": "admin-test", "password": "password-123"}).status_code == 200


def seed_ids(c):
    gear = c.get("/api/gear").json()
    by_name = {g["name"]: g for g in gear}
    return by_name


def test_setup_seed_and_auth(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        assert c.get("/api/gear").status_code == 401
        assert c.get("/api/status").json()["setup_required"] is True
        setup_admin(c)
        assert c.post("/api/setup", json={"username": "x-user", "password": "password-123"}).status_code == 409
        gear = c.get("/api/gear").json()
        names = {g["name"] for g in gear}
        assert {"Starling", "Heron", "Club 20", "Demo Drive", "Demo Delay", "DemoPick 0.73"} == names
        r = c.get("/")
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"


def test_seed_shows_both_chip_states(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        by_name = seed_ids(c)
        assert by_name["Starling"]["strings"]["state"] == "overdue"
        assert by_name["Starling"]["strings"]["days"] == 100
        assert by_name["Heron"]["strings"]["state"] == "fresh"
        sets = c.get("/api/sets").json()
        assert sets[0]["name"] == "Practice board"
        assert len(sets[0]["items"]) == 4


def test_login_rate_limit(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        for _ in range(5):
            r = c.post("/api/login", json={"username": "admin-test", "password": "wrong-pass"})
            assert r.status_code == 401
        r = c.post("/api/login", json={"username": "admin-test", "password": "wrong-pass"})
        assert r.status_code == 429
        assert "retry-after" in r.headers


def test_gear_crud_and_spec_validation(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        r = c.post("/api/gear", json={
            "type": "guitar", "name": "Testcaster", "make": "Acme", "model": "T-1",
            "year": 2020, "serial": "A123", "status": "home",
            "specs": {"finish": "Butterscotch", "tuning": "E standard", "bogus_key": "dropped"},
            "restring_interval_days": 60,
        })
        assert r.status_code == 201
        g = r.json()
        assert g["specs"]["finish"] == "Butterscotch"
        assert "bogus_key" not in g["specs"]
        assert g["strings"]["state"] == "never"

        gid = g["id"]
        r = c.patch(f"/api/gear/{gid}", json={"status": "luthier", "specs": {"mods": "New nut"}})
        assert r.status_code == 200
        g = r.json()
        assert g["status"] == "luthier"
        assert g["specs"]["mods"] == "New nut"
        assert g["specs"]["finish"] == "Butterscotch"  # patch merges specs

        # amps reject restring intervals
        r = c.post("/api/gear", json={"type": "amp", "name": "Amp", "restring_interval_days": 30})
        assert r.status_code == 400
        # unknown types rejected
        r = c.post("/api/gear", json={"type": "banjo", "name": "Banjo"})
        assert r.status_code == 422

        assert c.delete(f"/api/gear/{gid}").status_code == 200
        assert c.get(f"/api/gear/{gid}").status_code == 404


def test_restring_loop(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        gid = seed_ids(c)["Starling"]["id"]
        # logging a restring resets the chip and updates the current gauge
        r = c.post(f"/api/gear/{gid}/restrings", json={"brand": "Ernie Ball", "gauge": "11-48"})
        assert r.status_code == 201
        g = c.get(f"/api/gear/{gid}").json()
        assert g["strings"]["state"] == "fresh"
        assert g["strings"]["days"] == 0
        assert g["specs"]["string_gauge"] == "11-48"
        # due list is now empty for Starling
        due = c.get("/api/due").json()
        assert all(i["gear_id"] != gid for i in due)
        # history keeps both entries
        history = c.get(f"/api/gear/{gid}/restrings").json()
        assert len(history) == 2
        # future dates rejected
        assert c.post(f"/api/gear/{gid}/restrings", json={"date": "2999-01-01"}).status_code == 400
        # amps can't be restrung
        amp = seed_ids(c)["Club 20"]["id"]
        assert c.post(f"/api/gear/{amp}/restrings", json={}).status_code == 400
        # delete an entry
        rid = history[0]["id"]
        assert c.delete(f"/api/restrings/{rid}").status_code == 200
        assert len(c.get(f"/api/gear/{gid}/restrings").json()) == 1


def test_chip_thresholds(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        r = c.post("/api/gear", json={"type": "guitar", "name": "Chip Test", "restring_interval_days": 120})
        gid = r.json()["id"]  # interval 120, warn at 90 days; log oldest first so each is latest
        for days, state in [(130, "overdue"), (95, "aging"), (10, "fresh")]:
            c.post(f"/api/gear/{gid}/restrings", json={"date": days_ago(days)})
            g = c.get(f"/api/gear/{gid}").json()
            assert g["strings"]["state"] == state, (days, g["strings"])


def test_due_list(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        due = c.get("/api/due").json()
        assert [i["name"] for i in due] == ["Starling"]
        assert due[0]["state"] == "overdue"
        assert due[0]["days_until_due"] < 0
        # horizon picks up due-soon guitars: shorten Heron's interval so its
        # 20-day-old strings come due in 5 days
        heron = seed_ids(c)["Heron"]["id"]
        assert c.patch(f"/api/gear/{heron}", json={"restring_interval_days": 25}).status_code == 200
        names = {i["name"] for i in c.get("/api/due?days=7").json()}
        assert names == {"Starling", "Heron"}


def test_sets(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        by_name = seed_ids(c)
        r = c.post("/api/sets", json={"name": "Big board", "item_ids": [by_name["Demo Drive"]["id"], by_name["Demo Delay"]["id"]]})
        assert r.status_code == 201
        s = r.json()
        assert [i["name"] for i in s["items"]] == ["Demo Drive", "Demo Delay"]
        # duplicate name rejected
        assert c.post("/api/sets", json={"name": "big BOARD"}).status_code == 409
        # membership shows on the gear itself
        drive = c.get(f"/api/gear/{by_name['Demo Drive']['id']}").json()
        assert {s_["name"] for s_ in drive["sets"]} == {"Practice board", "Big board"}
        # replace items
        r = c.patch(f"/api/sets/{s['id']}", json={"item_ids": [by_name["Club 20"]["id"]]})
        assert [i["name"] for i in r.json()["items"]] == ["Club 20"]
        # deleting a set keeps the gear
        assert c.delete(f"/api/sets/{s['id']}").status_code == 200
        assert c.get(f"/api/gear/{by_name['Club 20']['id']}").status_code == 200
        # unknown gear in a set 404s
        assert c.post("/api/sets", json={"name": "Broken", "item_ids": [9999]}).status_code == 404


def test_feature_toggles(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        s = c.get("/api/settings").json()
        assert all(s[f"feature_{k}"] for k in ("guitars", "amps", "pedals", "picks", "sets", "maintenance"))
        r = c.put("/api/settings", json={"feature_picks": False, "feature_maintenance": False})
        assert r.status_code == 200
        assert r.json()["feature_picks"] is False
        assert r.json()["feature_maintenance"] is False
        # name change works and rejects blank
        assert c.put("/api/settings", json={"app_name": "My Rig"}).json()["app_name"] == "My Rig"
        assert c.put("/api/settings", json={"app_name": "  "}).status_code == 400


def test_users_and_admin_gate(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        r = c.post("/api/users", json={"username": "bandmate", "password": "password-456"})
        assert r.status_code == 201
        assert r.json()["is_admin"] is False
        # member can read gear but not touch settings/users
        with TestClient(main.app) as m:
            assert m.post("/api/login", json={"username": "bandmate", "password": "password-456"}).status_code == 200
            assert m.get("/api/gear").status_code == 200
            assert m.put("/api/settings", json={"feature_picks": False}).status_code == 403
            assert m.get("/api/users").status_code == 403
            assert m.put("/api/notifications", json={}).status_code == 403
        # admin can't demote or deactivate self
        me = c.get("/api/me").json()
        assert c.put(f"/api/users/{me['id']}", json={"is_admin": False}).status_code == 400
        # deactivating a user kills their session
        uid = r.json()["id"]
        assert c.put(f"/api/users/{uid}", json={"active": False}).status_code == 200
        assert m.get("/api/gear").status_code == 401


def test_token_api(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        r = c.post("/api/tokens", json={"name": "phone"})
        assert r.status_code == 201
        token = r.json()["token"]
        assert token.startswith("gs_")
        h = {"Authorization": f"Bearer {token}"}
        # reads
        assert c.get("/api/v1/gear", headers=h).status_code == 200
        assert len(c.get("/api/v1/gear?type=guitar", headers=h).json()) == 2
        # writes act as the token owner
        r = c.post("/api/v1/gear", headers=h, json={"type": "pick", "name": "Stubby 1.0", "specs": {"thickness": "1.0 mm", "quantity": 5}})
        assert r.status_code == 201
        gid = r.json()["id"]
        assert r.json()["added_by"] == "admin-test"
        assert c.patch(f"/api/v1/gear/{gid}", headers=h, json={"notes": "jar on the amp"}).status_code == 200
        # restring + due over the token API
        sg = seed_ids(c)["Starling"]["id"]
        assert c.post(f"/api/v1/gear/{sg}/restrings", headers=h, json={"gauge": "10-46"}).status_code == 201
        assert all(i["gear_id"] != sg for i in c.get("/api/v1/due", headers=h).json())
        # sets over the token API
        r = c.post("/api/v1/sets", headers=h, json={"name": "API set", "item_ids": [gid]})
        assert r.status_code == 201
        assert c.delete(f"/api/v1/sets/{r.json()['id']}", headers=h).status_code == 200
        # openapi + docs exist
        assert "/api/v1/gear" in c.get("/api/v1/openapi.json").json()["paths"]
        assert c.get("/api/docs").status_code == 200
        # bad tokens are rejected and then rate limited
        bad = {"Authorization": "Bearer gs_nope"}
        assert c.get("/api/v1/gear", headers=bad).status_code == 401
        # revoking kills the token
        tid = [t for t in c.get("/api/tokens").json() if t["name"] == "phone"][0]["id"]
        assert c.delete(f"/api/tokens/{tid}").status_code == 200
        assert c.get("/api/v1/gear", headers=h).status_code == 401


def test_photos(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        gid = seed_ids(c)["Club 20"]["id"]
        r = c.post(f"/api/gear/{gid}/photos", files={"photo": ("amp.png", PNG, "image/png")})
        assert r.status_code == 201
        url = r.json()["url"]
        g = c.get(f"/api/gear/{gid}").json()
        assert g["cover"] == url
        assert c.get(url).status_code == 200
        # garbage files rejected
        r = c.post(f"/api/gear/{gid}/photos", files={"photo": ("x.png", b"not an image", "image/png")})
        assert r.status_code == 400
        # delete via photo id
        pid = g["photos"][0]["id"]
        assert c.delete(f"/api/photos/{pid}").status_code == 200
        assert c.get(f"/api/gear/{gid}").json()["cover"] is None


def test_notifications_settings(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        r = c.get("/api/notifications")
        assert r.status_code == 200
        assert r.json()["notify_mode"] == "digest"
        assert c.put("/api/notifications", json={"notify_urls": "not-a-url"}).status_code == 400
        r = c.put("/api/notifications", json={"notify_urls": "ntfy://ntfy.example/rig", "notify_hour": 9})
        assert r.status_code == 200
        assert r.json()["notify_hour"] == 9
        # notification_items finds the seeded overdue guitar
        from datetime import datetime
        with main.db() as conn:
            items = main.notification_items(conn, datetime.now().astimezone())
            assert [i["name"] for i in items] == ["Starling"]
            # marking it announced silences it until the repeat window passes
            main.mark_announced(conn, "notify_state", items, datetime.now().astimezone())
            assert main.notification_items(conn, datetime.now().astimezone()) == []


def test_token_api_photo_delete_and_cover(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        h = {"Authorization": f"Bearer {c.post('/api/tokens', json={'name': 'bot'}).json()['token']}"}
        ids = seed_ids(c)
        amp, drive = ids["Club 20"]["id"], ids["Demo Drive"]["id"]
        first = c.post(f"/api/v1/gear/{amp}/photos", headers=h, files={"photo": ("a.png", PNG, "image/png")}).json()
        second = c.post(f"/api/v1/gear/{amp}/photos", headers=h, files={"photo": ("b.png", PNG, "image/png")}).json()
        other = c.post(f"/api/v1/gear/{drive}/photos", headers=h, files={"photo": ("c.png", PNG, "image/png")}).json()
        assert c.get(f"/api/v1/gear/{amp}", headers=h).json()["cover"] == first["url"]

        # both routes are in the published spec
        paths = c.get("/api/v1/openapi.json").json()["paths"]
        assert "delete" in paths["/api/v1/photos/{photo_id}"]
        assert "post" in paths["/api/v1/photos/{photo_id}/cover"]

        # auth: no token, a bad token, and a session cookie alone are all refused
        with TestClient(main.app) as anon:
            assert anon.delete(f"/api/v1/photos/{first['id']}").status_code == 401
            assert anon.post(f"/api/v1/photos/{first['id']}/cover").status_code == 401
            bad = {"Authorization": "Bearer gs_nope"}
            assert anon.delete(f"/api/v1/photos/{first['id']}", headers=bad).status_code == 401
            assert anon.post(f"/api/v1/photos/{first['id']}/cover", headers=bad).status_code == 401
        assert c.delete(f"/api/v1/photos/{first['id']}").status_code == 401

        # cover: the chosen photo becomes the cover, other gear is untouched
        assert c.post(f"/api/v1/photos/{second['id']}/cover", headers=h).json() == {"ok": True}
        g = c.get(f"/api/v1/gear/{amp}", headers=h).json()
        assert g["cover"] == second["url"]
        assert [p["id"] for p in g["photos"]] == [second["id"], first["id"]]
        assert c.get(f"/api/v1/gear/{drive}", headers=h).json()["cover"] == other["url"]
        assert c.post("/api/v1/photos/99999/cover", headers=h).status_code == 404

        # delete: removes the row and the file, cover falls back to the next photo
        assert c.delete(f"/api/v1/photos/{second['id']}", headers=h).json() == {"ok": True}
        assert c.get(second["url"]).status_code == 404
        g = c.get(f"/api/v1/gear/{amp}", headers=h).json()
        assert [p["id"] for p in g["photos"]] == [first["id"]]
        assert g["cover"] == first["url"]
        assert c.delete(f"/api/v1/photos/{second['id']}", headers=h).status_code == 404

        # ownership: gear is shared by everyone on the instance (same as the session routes),
        # so a member's token can manage photos on gear someone else added...
        member = c.post("/api/users", json={"username": "bandmate", "password": "password-456"}).json()
        with TestClient(main.app) as m:
            assert m.post("/api/login", json={"username": "bandmate", "password": "password-456"}).status_code == 200
            mh = {"Authorization": f"Bearer {m.post('/api/tokens', json={'name': 'member bot'}).json()['token']}"}
        assert c.post(f"/api/v1/photos/{other['id']}/cover", headers=mh).status_code == 200
        # ...until that member is deactivated (403) or the token is revoked (401)
        assert c.put(f"/api/users/{member['id']}", json={"active": False}).status_code == 200
        assert c.delete(f"/api/v1/photos/{other['id']}", headers=mh).status_code == 403
        tid = [t for t in c.get("/api/tokens").json() if t["name"] == "bot"][0]["id"]
        assert c.delete(f"/api/tokens/{tid}").status_code == 200
        assert c.delete(f"/api/v1/photos/{other['id']}", headers=h).status_code == 401
        assert c.get(f"/api/v1/gear/{drive}", headers=h).status_code == 401
        assert c.get(f"/api/gear/{drive}").json()["cover"] == other["url"]


def test_favorite_flag_round_trip_and_order(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        by_name = seed_ids(c)
        assert all(g["favorite"] is False for g in by_name.values())
        starling, heron = by_name["Starling"]["id"], by_name["Heron"]["id"]
        token = c.post("/api/tokens", json={"name": "fav"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}

        # v1 PATCH sets it, both APIs read it back
        r = c.patch(f"/api/v1/gear/{starling}", headers=h, json={"favorite": True})
        assert r.status_code == 200 and r.json()["favorite"] is True
        assert c.get(f"/api/v1/gear/{starling}", headers=h).json()["favorite"] is True
        assert c.get(f"/api/gear/{starling}").json()["favorite"] is True

        # favorites sort first within their type, then by name
        guitars = [g["name"] for g in c.get("/api/v1/gear?type=guitar", headers=h).json()]
        assert guitars == ["Starling", "Heron"]
        c.patch(f"/api/gear/{heron}", json={"favorite": True})
        c.patch(f"/api/gear/{starling}", json={"favorite": False})
        assert [g["name"] for g in c.get("/api/gear?type=guitar").json()] == ["Heron", "Starling"]
        types = [g["type"] for g in c.get("/api/gear").json()]
        assert types == sorted(types, key=["amp", "guitar", "pedal", "pick"].index)

        # other edits and a full PUT without the flag leave it alone; null is ignored
        c.patch(f"/api/gear/{heron}", json={"notes": "keeper"})
        full = c.get(f"/api/gear/{heron}").json()
        body = {k: full[k] for k in ("type", "name", "make", "model", "notes", "restring_interval_days")}
        assert c.put(f"/api/gear/{heron}", json=body).json()["favorite"] is True
        assert c.patch(f"/api/gear/{heron}", json={"favorite": None}).json()["favorite"] is True
        assert c.patch(f"/api/gear/{heron}", json={"favorite": "nope"}).status_code == 422

        # can be set on create
        r = c.post("/api/v1/gear", headers=h, json={"type": "pick", "name": "Fav Pick", "favorite": True})
        assert r.json()["favorite"] is True
        assert "favorite" in str(c.get("/api/v1/openapi.json").json()["components"]["schemas"]["GearPatch"])


def test_favorite_column_added_to_existing_database(tmp_path):
    import sqlite3

    main.DB_PATH = tmp_path / "old.db"
    main.PHOTOS_DIR = tmp_path / "photos"
    main.PHOTOS_DIR.mkdir()
    old = sqlite3.connect(main.DB_PATH)
    old.execute("""CREATE TABLE gear (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
        make TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', year INTEGER,
        serial TEXT NOT NULL DEFAULT '', specs TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT '',
        purchase_date TEXT, purchase_price REAL, notes TEXT NOT NULL DEFAULT '',
        restring_interval_days INTEGER, created_by INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    old.execute("INSERT INTO gear(type,name,created_at,updated_at) VALUES('amp','Old Amp','x','x')")
    old.commit()
    old.close()
    main.init_db()
    main.init_db()  # second start is a no-op
    with main.db() as c:
        row = c.execute("SELECT name, favorite FROM gear").fetchone()
    assert (row["name"], row["favorite"]) == ("Old Amp", 0)


def test_lifecycle_want_and_sold(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        by_name = seed_ids(c)
        assert all(g["lifecycle"] == "owned" for g in by_name.values())
        token = c.post("/api/tokens", json={"name": "life"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}

        want = c.post("/api/v1/gear", headers=h, json={
            "type": "guitar", "name": "Wish Guitar", "lifecycle": "want", "want_price": 1200,
        }).json()
        assert (want["lifecycle"], want["want_price"], want["strings"]) == ("want", 1200, None)

        starling = by_name["Starling"]["id"]
        assert any(i["gear_id"] == starling for i in c.get("/api/due?days=14").json())
        r = c.patch(f"/api/v1/gear/{starling}", headers=h, json={
            "lifecycle": "sold", "sold_date": "2026-01-15", "sold_price": 450.5,
        })
        assert r.status_code == 200
        assert (r.json()["lifecycle"], r.json()["sold_date"], r.json()["sold_price"]) == ("sold", "2026-01-15", 450.5)
        # sold gear leaves the due list and notifications but stays in the database
        assert all(i["gear_id"] != starling for i in c.get("/api/due?days=14").json())
        assert all(i["gear_id"] != starling for i in c.get("/api/v1/due?days=14", headers=h).json())
        assert [g["name"] for g in c.get("/api/gear?lifecycle=sold").json()] == ["Starling"]
        assert [g["name"] for g in c.get("/api/v1/gear?lifecycle=want", headers=h).json()] == ["Wish Guitar"]
        assert "Starling" not in [g["name"] for g in c.get("/api/gear?lifecycle=owned").json()]
        assert c.get("/api/gear?lifecycle=lost").status_code == 400
        assert c.patch(f"/api/gear/{starling}", json={"lifecycle": "stolen"}).status_code == 422
        assert c.patch(f"/api/gear/{starling}", json={"sold_date": "15/01/2026"}).status_code == 422
        # null leaves it alone; location status is separate and untouched
        assert c.patch(f"/api/gear/{starling}", json={"lifecycle": None}).json()["lifecycle"] == "sold"
        c.patch(f"/api/gear/{starling}", json={"status": "lent"})
        assert c.get(f"/api/gear/{starling}").json()["status"] == "lent"
        assert c.get(f"/api/gear/{starling}").json()["lifecycle"] == "sold"
        # bought the wish guitar
        assert c.patch(f"/api/gear/{want['id']}", json={"lifecycle": "owned"}).json()["lifecycle"] == "owned"


def test_old_database_gets_lifecycle_and_new_tables(tmp_path):
    import sqlite3

    main.DB_PATH = tmp_path / "old.db"
    main.PHOTOS_DIR = tmp_path / "photos"
    main.PHOTOS_DIR.mkdir()
    old = sqlite3.connect(main.DB_PATH)
    old.execute("""CREATE TABLE gear (id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
        make TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', year INTEGER,
        serial TEXT NOT NULL DEFAULT '', specs TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT '',
        purchase_date TEXT, purchase_price REAL, notes TEXT NOT NULL DEFAULT '',
        restring_interval_days INTEGER, favorite INTEGER NOT NULL DEFAULT 0, created_by INTEGER,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    old.execute("""INSERT INTO gear(type,name,status,favorite,specs,created_at,updated_at)
        VALUES('pedal','Old Pedal','lent',1,'{"voltage": "9V"}','x','x')""")
    old.commit()
    old.close()
    main.init_db()
    main.init_db()
    with main.db() as c:
        row = c.execute("SELECT * FROM gear").fetchone()
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert (row["name"], row["status"], row["favorite"], row["lifecycle"]) == ("Old Pedal", "lent", 1, "owned")
    assert row["sold_price"] is None and row["specs"] == '{"voltage": "9V"}'
    assert {"songs", "song_gear_settings", "song_device_patches", "song_effect_blocks", "song_photos", "shares"} <= tables


def test_controls_and_modeler_flag(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        by_name = seed_ids(c)
        drive = by_name["Demo Drive"]["id"]
        token = c.post("/api/tokens", json={"name": "ctl"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        r = c.patch(f"/api/v1/gear/{drive}", headers=h, json={"specs": {
            "controls": [{"name": "Gain"}, "Tone", {"name": "Level", "kind": "knob"},
                         {"name": "Voice", "kind": "switch"}, {"name": "gain"}, {"name": " "}],
            "modeler": True,
        }})
        assert r.status_code == 200
        g = r.json()
        assert [x["name"] for x in g["controls"]] == ["Gain", "Tone", "Level", "Voice"]
        assert g["controls"][3]["kind"] == "switch" and g["modeler"] is True
        # other spec edits keep the controls; the controls list replaces as a whole
        g = c.patch(f"/api/gear/{drive}", json={"specs": {"voltage": "18V"}}).json()
        assert len(g["controls"]) == 4 and g["specs"]["voltage"] == "18V"
        assert c.patch(f"/api/gear/{drive}", json={"specs": {"controls": [{"name": "X", "kind": "fader"}]}}).status_code == 400
        # picks have no controls, only pedals can be modelers
        pick = by_name["DemoPick 0.73"]["id"]
        assert c.patch(f"/api/gear/{pick}", json={"specs": {"controls": ["Gain"]}}).json()["controls"] == []
        amp = by_name["Club 20"]["id"]
        assert c.patch(f"/api/gear/{amp}", json={"specs": {"modeler": True}}).json()["modeler"] is False


def song_payload(by_name):
    return {
        "title": "Test Song", "artist": "Test Band", "tuning": "Drop D", "capo": 2, "key": "D", "bpm": 120,
        "guitar_id": by_name["Starling"]["id"], "amp_id": by_name["Club 20"]["id"], "set_id": 1,
        "notes": "Bridge pickup",
        "rig": [
            {"gear_id": by_name["Demo Drive"]["id"], "engaged": "toggle",
             "knobs": [{"name": "Gain", "value": "2:00"}, {"name": "Level", "value": 7}]},
            {"gear_id": by_name["Club 20"]["id"], "knobs": [{"name": "Master", "value": "noon"}]},
        ],
        "patches": [{
            "gear_id": by_name["Demo Delay"]["id"], "patch_ref": "12B", "patch_name": "Lead",
            "scenes": ["verse", "chorus", " "], "midi": {"channel": 1, "pc": 23},
            "blocks": [{"block_type": "Delay", "model": "Tape", "enabled": False,
                        "params": [{"name": "Mix", "value": "30%"}], "scene_overrides": {"solo": {"Mix": "40%"}}}],
        }],
    }


def test_song_crud_with_rig_and_patches(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        by_name = seed_ids(c)
        token = c.post("/api/tokens", json={"name": "songs"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        assert c.get("/api/v1/songs").status_code == 401

        r = c.post("/api/v1/songs", headers=h, json=song_payload(by_name))
        assert r.status_code == 201, r.text
        s = r.json()
        assert (s["guitar_name"], s["amp_name"], s["set_name"], s["key"]) == ("Starling", "Club 20", "Practice board", "D")
        assert [x["gear_name"] for x in s["rig"]] == ["Demo Drive", "Club 20"]
        assert s["rig"][0]["knobs"] == [{"name": "Gain", "value": "2:00"}, {"name": "Level", "value": "7"}]
        assert s["rig"][0]["engaged"] == "toggle"
        p = s["patches"][0]
        assert (p["patch_ref"], p["scenes"], p["midi"]) == ("12B", ["verse", "chorus"], {"channel": 1, "pc": 23})
        assert p["blocks"][0]["enabled"] is False and p["blocks"][0]["scene_overrides"] == {"solo": {"Mix": "40%"}}
        sid = s["id"]

        # list, search, and filter by gear used anywhere in the song
        assert [x["title"] for x in c.get("/api/songs").json()] == ["Test Song"]
        assert c.get("/api/v1/songs?q=band", headers=h).json()[0]["id"] == sid
        assert c.get(f"/api/songs?gear_id={by_name['Demo Delay']['id']}").json()[0]["id"] == sid
        assert c.get(f"/api/songs?gear_id={by_name['Heron']['id']}").json() == []

        # PATCH without rig keeps it; with rig replaces it
        r = c.patch(f"/api/v1/songs/{sid}", headers=h, json={"bpm": 90, "capo": None})
        assert (r.json()["bpm"], r.json()["capo"], len(r.json()["rig"])) == (90, None, 2)
        r = c.patch(f"/api/songs/{sid}", json={"rig": [{"gear_name": "Borrowed Fuzz", "knobs": [{"name": "Fuzz", "value": "max"}]}]})
        assert [x["gear_name"] for x in r.json()["rig"]] == ["Borrowed Fuzz"] and r.json()["rig"][0]["gear_id"] is None

        # nested rig routes
        r = c.post(f"/api/v1/songs/{sid}/rig", headers=h, json={"gear_id": by_name["Demo Drive"]["id"]})
        assert r.status_code == 201 and r.json()["position"] == 1
        rid = r.json()["id"]
        r = c.patch(f"/api/v1/songs/{sid}/rig/{rid}", headers=h, json={"engaged": "off", "knobs": [{"name": "Gain", "value": "9:00"}]})
        assert (r.json()["engaged"], r.json()["knobs"][0]["value"]) == ("off", "9:00")
        assert c.patch(f"/api/v1/songs/{sid}/rig/{rid}", headers=h, json={"engaged": "half"}).status_code == 422
        assert c.delete(f"/api/v1/songs/{sid}/rig/{rid}", headers=h).status_code == 200
        assert c.delete(f"/api/v1/songs/{sid}/rig/{rid}", headers=h).status_code == 404

        # nested patch routes
        pid = c.get(f"/api/songs/{sid}").json()["patches"][0]["id"]
        r = c.patch(f"/api/v1/songs/{sid}/patches/{pid}", headers=h, json={"patch_ref": "Bank 3 / Patch 2", "blocks": []})
        assert (r.json()["patch_ref"], r.json()["blocks"], r.json()["patch_name"]) == ("Bank 3 / Patch 2", [], "Lead")
        r = c.post(f"/api/songs/{sid}/patches", json={"gear_id": by_name["Demo Drive"]["id"], "patch_ref": "A1"})
        assert r.status_code == 201
        assert c.delete(f"/api/songs/{sid}/patches/{r.json()['id']}").status_code == 200

        # validation
        assert c.post("/api/songs", json={"title": ""}).status_code == 422
        assert c.post("/api/songs", json={"title": "X", "guitar_id": by_name["Club 20"]["id"]}).status_code == 400
        assert c.post("/api/songs", json={"title": "X", "rig": [{"knobs": []}]}).status_code == 400
        assert c.post("/api/songs", json={"title": "X", "guitar_id": 9999}).status_code == 400
        assert c.get("/api/songs/9999").status_code == 404
        assert "/api/v1/songs/{song_id}/rig/{setting_id}" in c.get("/api/v1/openapi.json").json()["paths"]


def test_song_keeps_gear_names_after_gear_is_deleted(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        by_name = seed_ids(c)
        sid = c.post("/api/songs", json=song_payload(by_name)).json()["id"]
        c.delete(f"/api/gear/{by_name['Club 20']['id']}")
        c.delete(f"/api/gear/{by_name['Demo Delay']['id']}")
        c.delete("/api/sets/1")
        s = c.get(f"/api/songs/{sid}").json()
        assert (s["amp_id"], s["amp_name"], s["set_id"], s["set_name"]) == (None, "Club 20", None, "Practice board")
        assert [(x["gear_id"], x["gear_name"]) for x in s["rig"]][1] == (None, "Club 20")
        assert (s["patches"][0]["gear_id"], s["patches"][0]["gear_name"]) == (None, "Demo Delay")


def test_song_photos_and_delete(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        sid = c.post("/api/songs", json={"title": "Photo Song"}).json()["id"]
        token = c.post("/api/tokens", json={"name": "sp"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        a = c.post(f"/api/songs/{sid}/photos", files={"photo": ("a.png", PNG, "image/png")}).json()
        b = c.post(f"/api/v1/songs/{sid}/photos", headers=h, files={"photo": ("b.png", PNG, "image/png")}).json()
        assert c.post(f"/api/songs/{sid}/photos", files={"photo": ("x.txt", b"hello", "text/plain")}).status_code == 400
        assert c.get(f"/api/songs/{sid}").json()["cover"] == a["url"]
        assert c.post(f"/api/v1/song-photos/{b['id']}/cover", headers=h).status_code == 200
        assert c.get("/api/songs").json()[0]["cover"] == b["url"]
        assert c.delete(f"/api/song-photos/{a['id']}").status_code == 200
        assert len(list(main.PHOTOS_DIR.iterdir())) == 1
        assert c.delete(f"/api/v1/songs/{sid}", headers=h).status_code == 200
        assert list(main.PHOTOS_DIR.iterdir()) == []
        assert c.get(f"/api/songs/{sid}").status_code == 404


def test_share_links_for_gear_and_sets(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        by_name = seed_ids(c)
        starling = by_name["Starling"]["id"]
        c.patch(f"/api/gear/{starling}", json={"serial": "SECRET-SERIAL", "purchase_price": 999, "notes": "Plays great <b>"})
        photo = c.post(f"/api/gear/{starling}/photos", files={"photo": ("a.png", PNG, "image/png")}).json()
        other = c.post(f"/api/gear/{by_name['Heron']['id']}/photos", files={"photo": ("b.png", PNG, "image/png")}).json()
        token = c.post("/api/tokens", json={"name": "share"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}

        assert c.get(f"/api/gear/{starling}").json()["share"] is None
        share = c.post(f"/api/v1/gear/{starling}/share", headers=h, json={"expires_in_days": 30}).json()["share"]
        assert len(share["token"]) >= 32 and share["expires_at"] and share["url"] == "/share/" + share["token"]
        assert c.get(f"/api/gear/{starling}").json()["share"]["token"] == share["token"]
        # asking again returns the same link
        assert c.post(f"/api/gear/{starling}/share").json()["share"]["token"] == share["token"]

        # the public page works signed out, is read-only, and hides serial/price
        anon = TestClient(main.app)
        page = anon.get(share["url"])
        assert page.status_code == 200
        assert "noindex" in page.headers["x-robots-tag"] and "nofollow" in page.headers["x-robots-tag"]
        assert '<meta name="robots" content="noindex' in page.text
        assert "Starling" in page.text and "3-tone sunburst" in page.text and "Plays great &lt;b&gt;" in page.text
        assert "SECRET-SERIAL" not in page.text and "999" not in page.text
        assert "<form" not in page.text and 'href="/#' not in page.text and "app.js" not in page.text
        name = photo["url"].rsplit("/", 1)[-1]
        assert anon.get(f"{share['url']}/photos/{name}").status_code == 200
        assert anon.get(f"{share['url']}/photos/{other['url'].rsplit('/', 1)[-1]}").status_code == 404
        assert anon.get(photo["url"]).status_code == 401
        assert anon.get("/share/not-a-real-token-at-all-000000").status_code == 404
        assert "Disallow: /" in anon.get("/robots.txt").text

        # regenerate kills the old URL, delete kills the link
        new = c.post(f"/api/gear/{starling}/share", json={"regenerate": True}).json()["share"]
        assert new["token"] != share["token"] and new["expires_at"] is None
        assert anon.get(share["url"]).status_code == 404
        assert anon.get(new["url"]).status_code == 200
        assert c.delete(f"/api/v1/gear/{starling}/share", headers=h).status_code == 200
        assert anon.get(new["url"]).status_code == 404
        assert c.get(f"/api/v1/gear/{starling}", headers=h).json()["share"] is None

        # expired links stop working
        s2 = c.post(f"/api/gear/{starling}/share", json={"expires_in_days": 1}).json()["share"]
        with main.db() as db:
            db.execute("UPDATE shares SET expires_at=? WHERE token=?", ("2000-01-01T00:00:00+00:00", s2["token"]))
        assert anon.get(s2["url"]).status_code == 404
        assert c.get(f"/api/gear/{starling}").json()["share"]["expired"] is True
        assert c.post(f"/api/gear/{starling}/share", json={"expires_in_days": 0}).status_code == 422

        # sets: one page listing the set's gear, with member photos only
        sset = c.post("/api/v1/sets/1/share", headers=h, json={}).json()["share"]
        page = anon.get(sset["url"])
        assert page.status_code == 200 and "Practice board" in page.text and "Demo Drive" in page.text
        assert "SECRET-SERIAL" not in page.text
        assert anon.get(f"{sset['url']}/photos/{name}").status_code == 200
        assert anon.get(f"{sset['url']}/photos/{other['url'].rsplit('/', 1)[-1]}").status_code == 404
        assert c.get("/api/sets/1").json()["share"]["token"] == sset["token"]
        c.delete("/api/sets/1")
        assert anon.get(sset["url"]).status_code == 404
        assert c.post("/api/gear/9999/share").status_code == 404


def test_song_and_lifecycle_feature_toggles(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        s = c.get("/api/settings").json()
        assert s["feature_songs"] and s["feature_want"] and s["feature_sold"]
        s = c.put("/api/settings", json={"feature_songs": False, "feature_sold": False}).json()
        assert (s["feature_songs"], s["feature_want"], s["feature_sold"]) == (False, True, False)


def make_preset(c, by_name, name="Crunch", artist="Band A", headers=None):
    r = c.post("/api/v1/presets" if headers else "/api/presets", headers=headers or {}, json={
        "name": name, "artist": artist, "amp_id": by_name["Club 20"]["id"],
        "rig": [{"gear_id": by_name["Demo Drive"]["id"], "engaged": "on", "knobs": [{"name": "Gain", "value": "2:00"}]}],
        "patches": [{"gear_name": "Floor modeler", "patch_ref": "12B", "scenes": ["verse", "solo"],
                     "blocks": [{"block_type": "Drive", "model": "Tube Screamer", "params": [{"name": "Drive", "value": "6"}]}]}],
    })
    assert r.status_code == 201, r.text
    return r.json()


def test_preset_is_a_live_link_from_songs(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        by_name = seed_ids(c)
        preset = make_preset(c, by_name)
        assert preset["amp_name"] == "Club 20"
        assert preset["rig"][0]["knobs"] == [{"name": "Gain", "value": "2:00"}]
        assert preset["patches"][0]["blocks"][0]["model"] == "Tube Screamer"
        songs = []
        for title in ("First", "Second"):
            r = c.post("/api/songs", json={"title": title, "artist": "Band A",
                                           "presets": [{"preset_id": preset["id"], "label": "Verse"}]})
            assert r.status_code == 201
            songs.append(r.json())
        assert songs[0]["presets"][0]["label"] == "Verse"
        assert songs[0]["presets"][0]["preset"]["name"] == "Crunch"
        # one edit to the preset shows up in every song using it
        r = c.patch(f"/api/presets/{preset['id']}", json={
            "name": "Big crunch",
            "rig": [{"gear_id": by_name["Demo Drive"]["id"], "knobs": [{"name": "Gain", "value": "max"}]}],
        })
        assert r.status_code == 200
        for s in songs:
            full = c.get(f"/api/songs/{s['id']}").json()
            assert full["presets"][0]["preset"]["name"] == "Big crunch"
            assert full["presets"][0]["preset"]["rig"][0]["knobs"][0]["value"] == "max"
            assert full["presets"][0]["preset"]["patches"][0]["patch_ref"] == "12B"
        got = c.get(f"/api/presets/{preset['id']}").json()
        assert got["song_count"] == 2 and {s["title"] for s in got["songs"]} == {"First", "Second"}
        assert c.get("/api/songs").json()[0]["preset_names"] == ["Big crunch"]
        # songs using a preset show up on the gear page of gear inside the preset
        drive_songs = c.get(f"/api/songs?gear_id={by_name['Demo Drive']['id']}").json()
        assert {s["title"] for s in drive_songs} == {"First", "Second"}
        # PATCH without presets keeps them; an empty list clears them
        c.patch(f"/api/songs/{songs[0]['id']}", json={"bpm": 90})
        assert len(c.get(f"/api/songs/{songs[0]['id']}").json()["presets"]) == 1
        c.patch(f"/api/songs/{songs[0]['id']}", json={"presets": []})
        assert c.get(f"/api/songs/{songs[0]['id']}").json()["presets"] == []
        # unknown preset ids are rejected
        assert c.post("/api/songs", json={"title": "Bad", "presets": [{"preset_id": 999}]}).status_code == 404
        # deleting the preset leaves the song, just without the link
        assert c.delete(f"/api/presets/{preset['id']}").json() == {"ok": True}
        assert c.get(f"/api/songs/{songs[1]['id']}").json()["presets"] == []
        assert c.get(f"/api/presets/{preset['id']}").status_code == 404
        assert c.patch("/api/presets/999", json={"name": "x"}).status_code == 404


def test_save_song_as_preset_and_copy_back(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        by_name = seed_ids(c)
        song = c.post("/api/songs", json={
            "title": "Own chain", "artist": "Band B", "amp_id": by_name["Club 20"]["id"],
            "rig": [{"gear_id": by_name["Demo Delay"]["id"], "engaged": "toggle", "knobs": [{"name": "Time", "value": "noon"}]}],
            "patches": [{"gear_name": "Floor modeler", "patch_ref": "3A", "blocks": [{"block_type": "Reverb"}]}],
        }).json()
        # save a copy only: the song keeps its own chain
        r = c.post(f"/api/songs/{song['id']}/save-as-preset", json={"name": "Ambient"})
        assert r.status_code == 201
        kept = r.json()
        assert kept["artist"] == "Band B" and kept["amp_name"] == "Club 20" and kept["song_count"] == 0
        assert kept["rig"][0]["engaged"] == "toggle" and kept["patches"][0]["blocks"][0]["block_type"] == "Reverb"
        assert len(c.get(f"/api/songs/{song['id']}").json()["rig"]) == 1
        # save and switch the song over to the preset
        swapped = c.post(f"/api/songs/{song['id']}/save-as-preset", json={"name": "Ambient 2", "use_in_song": True}).json()
        full = c.get(f"/api/songs/{song['id']}").json()
        assert full["rig"] == [] and full["patches"] == []
        assert [p["preset"]["name"] for p in full["presets"]] == ["Ambient 2"]
        # copy it back into the song to tweak it for this song only
        link = full["presets"][0]["id"]
        copied = c.post(f"/api/songs/{song['id']}/presets/{link}/copy").json()
        assert copied["presets"] == []
        assert copied["rig"][0]["knobs"] == [{"name": "Time", "value": "noon"}]
        assert copied["patches"][0]["blocks"][0]["block_type"] == "Reverb"
        # the preset itself is untouched
        assert len(c.get(f"/api/presets/{swapped['id']}").json()["rig"]) == 1
        assert c.post(f"/api/songs/{song['id']}/presets/{link}/copy").status_code == 404


def test_artist_grouping(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        by_name = seed_ids(c)
        for title, artist in (("B side", "the band"), ("A side", "The Band "), ("Solo", "Zed"), ("Loose", "")):
            assert c.post("/api/songs", json={"title": title, "artist": artist}).status_code == 201
        make_preset(c, by_name, "Band tone", "THE BAND")
        make_preset(c, by_name, "Generic", "")
        groups = c.get("/api/artists").json()
        names = [g["artist"] for g in groups]
        assert names[-1] == ""  # songs with no artist come last
        band = groups[0]
        assert band["artist"].lower() == "the band" and band["artist"] != "the band"
        assert (band["song_count"], band["preset_count"]) == (2, 1)
        assert [s["title"] for s in band["songs"]] == ["A side", "B side"]
        assert groups[1]["artist"] == "Zed"
        assert (groups[-1]["song_count"], groups[-1]["preset_count"]) == (1, 1)
        assert len(c.get("/api/songs?artist=the%20BAND").json()) == 2
        assert [p["name"] for p in c.get("/api/presets?artist=the band").json()] == ["Band tone"]
        assert [p["name"] for p in c.get("/api/presets?q=gen").json()] == ["Generic"]


def test_presets_and_artists_over_token_api(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        by_name = seed_ids(c)
        token = c.post("/api/tokens", json={"name": "ai"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        assert c.get("/api/v1/presets").status_code == 401
        preset = make_preset(c, by_name, headers=h)
        song = c.post("/api/v1/songs", headers=h, json={"title": "Via API", "presets": [{"preset_id": preset["id"]}]})
        assert song.status_code == 201
        assert c.get("/api/v1/artists", headers=h).json()[0]["preset_count"] == 1
        assert c.patch(f"/api/v1/presets/{preset['id']}", headers=h, json={"notes": "bridge pickup"}).status_code == 200
        assert c.get(f"/api/v1/songs/{song.json()['id']}", headers=h).json()["presets"][0]["preset"]["notes"] == "bridge pickup"
        # amp must be an amp
        bad = c.post("/api/v1/presets", headers=h, json={"name": "x", "amp_id": by_name["Starling"]["id"]})
        assert bad.status_code == 400


def test_existing_songs_database_gets_preset_tables(tmp_path):
    fresh(tmp_path)
    with TestClient(main.app) as c:
        setup_admin(c)
        c.post("/api/songs", json={"title": "Kept", "rig": [{"gear_name": "Some pedal", "knobs": [{"name": "Level", "value": "1:00"}]}]})
    with main.db() as conn:
        for t in ("song_presets", "preset_effect_blocks", "preset_device_patches", "preset_gear_settings", "presets"):
            conn.execute(f"DROP TABLE {t}")
    main.init_db()  # a v0.2.0 database starting up on the new version
    with main.db() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        gear_count = conn.execute("SELECT COUNT(*) FROM gear").fetchone()[0]
    assert {"presets", "preset_gear_settings", "preset_device_patches", "preset_effect_blocks", "song_presets"} <= tables
    assert gear_count == 6
    with TestClient(main.app) as c:
        c.post("/api/login", json={"username": "admin-test", "password": "password-123"})
        song = c.get("/api/songs").json()[0]
        assert song["title"] == "Kept" and song["preset_names"] == []
        assert c.get(f"/api/songs/{song['id']}").json()["rig"][0]["knobs"][0]["value"] == "1:00"

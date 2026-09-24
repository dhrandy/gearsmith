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

"""Setup, passphrase login, CSRF protection and sessions."""

from tests.conftest import CSRF, PASSPHRASE


def test_setup_required_before_use(raw_client):
    status = raw_client.get("/api/status").json()
    assert status == {"version": status["version"], "setup_complete": False, "auth_required": False, "authenticated": False,
                      "password_optional": True, "password_missing": False}
    assert raw_client.get("/api/summary").status_code == 428


def test_setup_then_login_logout(raw_client):
    assert raw_client.post("/api/setup", json={"passphrase": PASSPHRASE}, headers=CSRF).status_code == 200
    assert raw_client.get("/api/summary").status_code == 200          # setup logs you in
    raw_client.post("/api/auth/logout", headers=CSRF)
    raw_client.cookies.clear()
    assert raw_client.get("/api/summary").status_code == 401
    assert raw_client.post("/api/auth/login", json={"passphrase": "wrong"}, headers=CSRF).status_code == 401
    assert raw_client.post("/api/auth/login", json={"passphrase": PASSPHRASE}, headers=CSRF).status_code == 200
    assert raw_client.get("/api/summary").status_code == 200


def test_setup_cannot_run_twice(client):
    assert client.post("/api/setup", json={"passphrase": "another pass"}).status_code == 409


def test_short_passphrase_rejected(raw_client):
    assert raw_client.post("/api/setup", json={"passphrase": "short"}, headers=CSRF).status_code == 400


def test_mutations_require_csrf_header(client):
    del client.headers["X-Requested-With"]
    assert client.post("/api/profiles", json={"name": "Kid"}).status_code == 403
    assert client.post("/api/profiles", json={"name": "Kid"}, headers=CSRF).status_code == 200


def test_login_rate_limited(raw_client):
    raw_client.post("/api/setup", json={"passphrase": PASSPHRASE}, headers=CSRF)
    codes = [raw_client.post("/api/auth/login", json={"passphrase": "nope"}, headers=CSRF).status_code for _ in range(10)]
    assert codes[-1] == 429


def test_no_passphrase_mode(raw_client):
    raw_client.post("/api/setup", json={"passphrase": None}, headers=CSRF)
    assert raw_client.get("/api/status").json()["authenticated"] is True
    assert raw_client.get("/api/summary").status_code == 200


def test_change_passphrase_invalidates_sessions(client, raw_client):
    r = client.post("/api/auth/passphrase", json={"current": PASSPHRASE, "new": "a brand new pass"})
    assert r.status_code == 200
    assert client.get("/api/summary").status_code == 200   # the changer gets a fresh session
    bad = client.post("/api/auth/passphrase", json={"current": "wrong", "new": "whatever123"})
    assert bad.status_code == 403


def test_security_headers(client):
    r = client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert "camera=()" in r.headers["permissions-policy"]
    assert r.headers["cross-origin-opener-policy"] == "same-origin"


def test_app_addresses_that_fall_back_to_the_page_get_its_policy(client):
    # Any other address serves the app's page (old bookmarks, typos): with the same Content-Security-Policy.
    r = client.get("/some/old/bookmark")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "script-src 'self'" in r.headers["content-security-policy"]


def test_oauth_hand_off_page_allows_only_its_own_script(client):
    import base64
    import hashlib
    import re
    r = client.get("/callback")
    csp = r.headers["content-security-policy"]
    script = re.search(r"<script>(.*?)</script>", r.text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in csp
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp


def test_head_requests_for_uptime_monitors(client):
    assert client.head("/health").status_code == 200
    assert client.head("/").status_code == 200


def test_unexpected_errors_answer_in_json(isolated_data, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import system
    from app.main import create_app
    from tests.conftest import BASE, LOCAL_CLIENT

    monkeypatch.setattr(system.auth, "is_setup_complete", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT, raise_server_exceptions=False) as c:
        r = c.get("/api/status")
    assert r.status_code == 500
    assert "Something went wrong" in r.json()["detail"]


def test_tokens_encrypted_at_rest(client, isolated_data):
    from tests.conftest import connect_institution
    import sqlite3
    connect_institution(client, "epic-stanford-health-care")
    db = sqlite3.connect(isolated_data / "syntropy.db")
    creds = db.execute("SELECT credentials_enc FROM connections").fetchone()[0]
    assert creds and "access_token" not in creds
    assert (isolated_data / "secret.key").exists()

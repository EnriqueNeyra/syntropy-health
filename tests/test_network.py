"""Reaching this server from other devices: addresses, the syntropyhealth.local name, and the desktop apps' switch."""

from app.services import network
from tests.conftest import CSRF


def test_servers_report_where_they_listen_but_cant_be_switched(client):
    net = client.get("/api/system/network").json()
    assert net["managed"] is False and net["on_network"] is False and net["addresses"] == []
    assert net["password_set"] is True and net["local_url"].startswith("http://localhost:")
    r = client.put("/api/system/network", json={"enabled": True})
    assert r.status_code == 409 and "where it's started" in r.json()["detail"]


def test_desktop_switch_asks_for_a_password_first(raw_client, monkeypatch):
    raw_client.post("/api/setup", json={"profile_name": "Alex"}, headers=CSRF)     # no password
    calls = []
    monkeypatch.setattr(network, "_controller", calls.append)
    r = raw_client.put("/api/system/network", json={"enabled": True}, headers=CSRF)
    assert r.status_code == 400 and "password" in r.json()["detail"] and calls == []
    r = raw_client.put("/api/system/network", json={"enabled": True, "without_password": True}, headers=CSRF)
    assert r.json() == {"ok": True, "restarting": True} and calls == [True]
    events = raw_client.get("/api/audit").json()["events"]
    assert any(e["action"] == "network.enabled" for e in events)


def test_desktop_switch_with_a_password(client, monkeypatch):
    calls = []
    monkeypatch.setattr(network, "_controller", calls.append)
    assert client.put("/api/system/network", json={"enabled": True}).json()["restarting"] is True
    monkeypatch.setenv("SYNTROPY_LISTEN_HOST", "0.0.0.0")
    assert client.put("/api/system/network", json={"enabled": True}).json()["restarting"] is False   # already on
    assert client.put("/api/system/network", json={"enabled": False}).json()["restarting"] is True
    assert calls == [True, False]


def test_switch_needs_sign_in(raw_client, monkeypatch):
    raw_client.post("/api/setup", json={"password": "correct horse battery", "profile_name": "Alex"}, headers=CSRF)
    raw_client.cookies.clear()
    monkeypatch.setattr(network, "_controller", lambda on: None)
    assert raw_client.put("/api/system/network", json={"enabled": True, "without_password": True}, headers=CSRF).status_code == 401
    assert raw_client.get("/api/system/network").status_code == 401


def test_addresses_shown_when_listening_on_the_network(client, monkeypatch):
    monkeypatch.setenv("SYNTROPY_LISTEN_HOST", "0.0.0.0")
    monkeypatch.setenv("SYNTROPY_LISTEN_PORT", "8000")
    monkeypatch.setattr(network, "home_addresses", lambda: ["192.168.1.20"])
    monkeypatch.setattr(network, "tailscale_addresses", lambda: ["100.101.102.103"])
    monkeypatch.setattr(network, "tailscale_name", lambda: "mini.tail1234.ts.net")
    monkeypatch.setattr(network.advertiser, "name", "syntropyhealth.local")
    net = client.get("/api/system/network").json()
    assert [a["url"] for a in net["addresses"]] == [
        "http://syntropyhealth.local:8000", "http://192.168.1.20:8000",
        "http://mini.tail1234.ts.net:8000", "http://100.101.102.103:8000"]
    assert net["local_name"] == "syntropyhealth.local" and net["tailscale"] is True


def test_home_addresses_leave_out_tailscale_vpns_and_virtual_machines(monkeypatch):
    monkeypatch.setattr(network, "primary_address", lambda: "192.168.1.20")
    monkeypatch.setattr(network, "_interfaces", lambda: [
        ("lo0", "127.0.0.1"), ("en0", "192.168.1.20"), ("utun7", "100.101.102.103"), ("feth1214", "10.147.18.33"),
        ("docker0", "172.17.0.1"), ("en1", "10.0.0.8"), ("en5", "169.254.10.2")])
    assert network.home_addresses() == ["192.168.1.20", "10.0.0.8"]
    assert network.tailscale_addresses() == ["100.101.102.103"]


def test_announcing_is_skipped_unless_listening_on_the_network(monkeypatch):
    started = []
    monkeypatch.setattr(network.advertiser, "start", started.append)
    monkeypatch.setenv("SYNTROPY_LISTEN_PORT", "8000")
    monkeypatch.setenv("SYNTROPY_LISTEN_HOST", "127.0.0.1")
    network.start_announcing()
    monkeypatch.setenv("SYNTROPY_LISTEN_HOST", "0.0.0.0")
    network.start_announcing()
    assert started == [8000]


def test_onboarding_follows_setup(raw_client):
    raw_client.post("/api/setup", json={"password": "correct horse battery", "profile_name": "Alex"}, headers=CSRF)
    raw_client.headers.update(CSRF)
    assert raw_client.get("/api/status").json()["onboarding"] is True
    assert raw_client.post("/api/onboarding/done").json() == {"ok": True}
    assert raw_client.get("/api/status").json()["onboarding"] is False


def test_onboarding_can_be_shown_again(client):
    assert client.get("/api/status").json()["onboarding"] is True
    client.post("/api/onboarding/done")
    client.post("/api/onboarding/restart")
    assert client.get("/api/status").json()["onboarding"] is True


def test_without_a_password_only_local_names_reach_the_api(raw_client):
    raw_client.post("/api/setup", json={"profile_name": "Alex"}, headers=CSRF)
    for host in ("localhost:8000", "127.0.0.1:8000", "192.168.1.20:8000", "100.101.102.103", "syntropyhealth.local:8000",
                 "mini.tail1234.ts.net:8000", "nas:8000", "[::1]:8000"):
        assert raw_client.get("/api/status", headers={"host": host}).status_code == 200, host
    for host in ("evil.example.com", "evil.example.com:8000", "127.0.0.1.nip.io:8000", "8.8.8.8"):
        assert raw_client.get("/api/status", headers={"host": host}).status_code == 421, host
    assert raw_client.get("/health", headers={"host": "evil.example.com"}).status_code == 200    # not the API


def test_with_a_password_any_address_works(client, monkeypatch):
    assert client.get("/api/status", headers={"host": "health.example.com"}).status_code == 200


def test_extra_allowed_hosts(raw_client, monkeypatch):
    raw_client.post("/api/setup", json={"profile_name": "Alex"}, headers=CSRF)
    monkeypatch.setenv("SYNTROPY_ALLOWED_HOSTS", "health.example.com")
    assert raw_client.get("/api/status", headers={"host": "health.example.com"}).status_code == 200

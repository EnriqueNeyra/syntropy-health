"""Update checks: comparing versions, telling the owner, and installing in the apps. GitHub is never contacted."""

from __future__ import annotations

import time

import pytest

from app.core import config, settings
from app.services import updates

RELEASE = {
    "tag_name": "v9.1.0", "html_url": "https://github.com/EnriqueNeyra/syntropy-health/releases/tag/v9.1.0",
    "body": "What's new", "published_at": "2026-10-01T12:00:00Z",
    "assets": [{"name": "syntropy_health-9.1.0-py3-none-any.whl", "browser_download_url": "https://example.test/w.whl"},
               {"name": updates.MAC_ASSET, "browser_download_url": "https://example.test/mac.dmg"}],
}


@pytest.fixture
def latest(monkeypatch):
    """GitHub answers with RELEASE (or whatever a test sets), and counts how often it was asked."""
    answer = {"release": updates._release(RELEASE), "calls": 0}

    def fake():
        answer["calls"] += 1
        if isinstance(answer["release"], Exception):
            raise answer["release"]
        return answer["release"]

    monkeypatch.setattr(updates, "fetch_latest", fake)
    return answer


@pytest.fixture(autouse=True)
def no_installer():
    updates.set_installer(None)
    updates.set_progress(None)
    yield
    updates.set_installer(None)
    updates.set_progress(None)


def test_versions():
    assert updates.version_tuple("v1.2") == (1, 2, 0)
    assert updates.version_tuple("1.3.0rc1") == (1, 3, 0)
    assert updates.is_newer("1.0.1", "1.0.0") and updates.is_newer("v2.0", "1.9.9")
    assert not updates.is_newer("1.0", "1.0.0") and not updates.is_newer("0.9.0", "1.0.0")
    assert not updates.is_newer("nightly", "1.0.0")


def test_package_source_prefers_the_wheel():
    rel = updates._release(RELEASE)
    assert updates.package_source(rel) == "syntropy-health @ https://example.test/w.whl"
    rel["assets"] = {}
    assert updates.package_source(rel).endswith("/archive/refs/tags/v9.1.0.tar.gz")


def test_owner_sees_an_update(client, latest):
    before = client.get("/api/system/updates").json()
    assert before["current"] == config.APP_VERSION and not before["available"] and before["check"] and not before["auto"]
    assert "update" not in client.get("/api/status").json()

    u = client.post("/api/system/updates/check").json()
    assert u["available"] and u["latest"]["version"] == "9.1.0" and u["latest"]["notes"] == "What's new"
    assert "assets" not in u["latest"]
    assert u["method"]["kind"] == "source" and u["method"]["command"].startswith("git pull")
    assert client.get("/api/status").json()["update"] == {"version": "9.1.0", "installs": False}

    # Turning checks off hides the notice.
    off = client.put("/api/system/updates", json={"check": False}).json()
    assert not off["check"]
    assert "update" not in client.get("/api/status").json()


def test_check_failure_is_remembered(client, latest):
    latest["release"] = updates.CheckFailed("GitHub couldn't be reached.")
    u = client.post("/api/system/updates/check").json()
    assert u["error"] == "GitHub couldn't be reached." and u["checked_at"] and not u["available"]


def test_no_release_yet(client, latest):
    latest["release"] = None
    u = client.post("/api/system/updates/check").json()
    assert u["latest"] is None and not u["available"] and not u["error"]


def test_env_turns_checks_off(client, latest, monkeypatch):
    monkeypatch.setenv("SYNTROPY_UPDATE_CHECK", "false")
    assert client.post("/api/system/updates/check").status_code == 409
    assert not client.get("/api/system/updates").json()["check_allowed"]


def test_docker_and_installer_methods(client, monkeypatch):
    monkeypatch.setenv("SYNTROPY_INSTALL", "docker")
    m = client.get("/api/system/updates").json()["method"]
    assert m == {"kind": "docker", "installs": False, "automatic": False, "command": "docker compose pull && docker compose up -d"}
    # Only an installation that installs updates itself offers automatic updates.
    assert client.put("/api/system/updates", json={"auto": True}).status_code == 409


def test_app_installs_updates(client, latest):
    got = []

    def installer(release, automatic):
        got.append((release["version"], automatic))
        updates.set_progress("installing")

    updates.set_installer(installer)
    assert client.post("/api/system/updates/install").status_code == 409     # nothing newer known yet
    client.post("/api/system/updates/check")
    assert client.get("/api/status").json()["update"]["installs"] is True
    u = client.put("/api/system/updates", json={"auto": True}).json()
    assert u["auto"] and u["method"] == {"kind": "app", "installs": True, "automatic": True}
    client.post("/api/system/updates/install")
    for _ in range(50):
        if got:
            break
        time.sleep(0.02)
    assert got == [("9.1.0", False)]
    assert client.get("/api/system/updates").json()["progress"]["state"] == "installing"
    # A second request while it's installing doesn't start another.
    client.post("/api/system/updates/install")
    time.sleep(0.05)
    assert len(got) == 1


def test_failed_install_is_shown(client, latest):
    def installer(release, automatic):
        raise RuntimeError("The download didn't match the release's checksum, so it wasn't installed.")

    updates.set_installer(installer)
    client.post("/api/system/updates/check")
    client.post("/api/system/updates/install")
    for _ in range(50):
        progress = client.get("/api/system/updates").json()["progress"]
        if progress and progress["state"] == "failed":
            break
        time.sleep(0.02)
    assert progress["state"] == "failed" and "checksum" in progress["detail"]


async def test_daily_tick_checks_once_and_installs_automatically(client, latest):
    got = []
    updates.set_installer(lambda release, automatic: got.append(automatic))
    settings.set("updates.auto", True)
    await updates.tick()
    await updates.tick()
    assert latest["calls"] == 1
    for _ in range(50):
        if got:
            break
        time.sleep(0.02)
    assert got == [True]


def test_only_the_owner(raw_client, latest):
    from tests.test_accounts import _join, _person, _signed_in_owner

    owner = _signed_in_owner(raw_client)
    owner.post("/api/system/updates/check")
    assert owner.get("/api/status").json()["update"]["version"] == "9.1.0"
    theirs, _ = _join(owner, _person(owner, "Maria"))
    assert theirs.get("/api/system/updates").status_code == 403
    assert theirs.post("/api/system/updates/install").status_code == 403
    assert "update" not in theirs.get("/api/status").json()


def test_update_due(client, latest, capsys):
    from app import cli

    with pytest.raises(SystemExit) as off:
        cli.update_due()
    assert off.value.code == 1 and "off" in capsys.readouterr().out
    settings.set("updates.auto", True)
    cli.update_due()        # a newer release and automatic updates on: exits normally
    assert "9.1.0 is available" in capsys.readouterr().out
    latest["release"] = updates._release({**RELEASE, "tag_name": f"v{config.APP_VERSION}"})
    with pytest.raises(SystemExit):
        cli.update_due()

"""The Mac and Windows apps' updater (desktop/updater.py): what it downloads is checked before anything is installed."""

from __future__ import annotations

import hashlib
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))
import updater  # noqa: E402

from app.services import updates  # noqa: E402

APP = b"the new app"


def _release(sums: str) -> dict:
    return {"version": "9.1.0", "url": "https://example.test/release",
            "assets": {updates.MAC_ASSET: "https://example.test/mac.dmg", updates.CHECKSUMS_ASSET: sums}}


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """An updater on a "Mac" whose downloads come from memory and whose install step only records what it got."""
    files = {"https://example.test/mac.dmg": APP}
    monkeypatch.setattr(updater, "_download", lambda url, dest: dest.write_bytes(files[url] if url in files else url.encode()))
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    state = {"hidden": False, "installed": [], "progress": [], "quit": threading.Event()}
    monkeypatch.setattr(updater.Updater, "_install_mac",
                        lambda self, file, background: state["installed"].append((file.read_bytes(), background)))
    monkeypatch.setattr(updater.time, "sleep", lambda s: state.update(hidden=True))    # the window closes meanwhile
    u = updater.Updater(tmp_path / "updates", lambda: state["hidden"], state["quit"].set,
                        lambda s, d: state["progress"].append(s))
    return u, state


def test_installs_a_checked_download(mac):
    u, state = mac
    u(_release(f"{hashlib.sha256(APP).hexdigest()}  {updates.MAC_ASSET}\n"), False)
    assert state["installed"] == [(APP, False)]
    assert state["progress"] == ["downloading", "installing"]
    assert state["quit"].wait(3)


def test_automatic_updates_wait_for_the_window_to_close(mac):
    u, state = mac
    u(_release(f"{hashlib.sha256(APP).hexdigest()} *{updates.MAC_ASSET}\n"), True)
    assert state["progress"] == ["downloading", "waiting", "installing"]
    assert state["installed"] == [(APP, True)]       # reopens in the background, as it was


def test_refuses_a_download_that_doesnt_match(mac):
    u, state = mac
    with pytest.raises(updater.UpdateError, match="checksum"):
        u(_release(f"{'0' * 64}  {updates.MAC_ASSET}\n"), False)
    with pytest.raises(updater.UpdateError, match="checksum"):
        u(_release("no entry for this file\n"), False)
    assert not state["installed"] and not state["quit"].is_set()


def test_a_release_without_this_computers_app(mac):
    u, _ = mac
    with pytest.raises(updater.UpdateError, match="no app for this computer"):
        u({"version": "9.1.0", "url": "https://example.test/release", "assets": {}}, False)

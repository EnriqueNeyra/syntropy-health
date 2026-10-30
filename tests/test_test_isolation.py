"""The suite's own promises: offline, and unaffected by what a developer has set."""

from __future__ import annotations

import os
import socket
import threading

import pytest

from tests.conftest import BlockedNetwork, _network_attempts


@pytest.mark.parametrize("reach", [
    lambda: socket.getaddrinfo("example.com", 443),
    lambda: socket.create_connection(("192.0.2.1", 80), timeout=0.1),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("192.0.2.1", 53)),
])
def test_anything_but_this_computer_is_blocked(reach):
    with pytest.raises(BlockedNetwork):
        reach()
    assert _network_attempts
    _network_attempts.clear()     # expected here; anywhere else it fails the test


def test_this_computer_is_allowed():
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        answer = threading.Thread(target=lambda: server.accept()[0].close(), daemon=True)
        answer.start()
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        socket.getaddrinfo("localhost", port)
        answer.join(2)


def test_a_developers_settings_are_not_seen():
    assert os.environ["SYNTROPY_NO_DOTENV"] == "1"
    assert "SYNTROPY_UPDATE_CHECK" not in os.environ and "SYNTROPY_INSTALL" not in os.environ


def test_env_file_is_loaded_only_outside_the_tests(tmp_path, monkeypatch):
    from app.core import config
    (tmp_path / ".env").write_text("SYNTROPY_TEST_ONLY_SETTING=from-the-file\n# a comment\n")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("SYNTROPY_TEST_ONLY_SETTING", raising=False)
    config._load_env_file()
    assert "SYNTROPY_TEST_ONLY_SETTING" not in os.environ
    monkeypatch.delenv("SYNTROPY_NO_DOTENV")
    try:
        config._load_env_file()
        assert os.environ["SYNTROPY_TEST_ONLY_SETTING"] == "from-the-file"
    finally:
        os.environ.pop("SYNTROPY_TEST_ONLY_SETTING", None)

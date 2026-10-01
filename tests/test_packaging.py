"""The syntropy-health command, where data lives when installed, and giving the iPhone a reachable address."""

import sys
import zipfile
from pathlib import Path

from app import cli, mcp_server
from app.core import config
from app.services import network


def test_data_folder_follows_how_it_was_installed(monkeypatch, tmp_path):
    monkeypatch.delenv("SYNTROPY_DATA_DIR", raising=False)
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    assert config.is_source_checkout() and config.default_data_dir() == config.REPO_ROOT / "data"
    monkeypatch.setattr(config, "is_source_checkout", lambda: False)
    for platform, expected in (("darwin", tmp_path / "Library/Application Support/Syntropy Health"),
                               ("linux", tmp_path / ".local/share/syntropy-health")):
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert config.default_data_dir() == expected
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData/Local"))
    assert config.default_data_dir() == tmp_path / "AppData/Local/Syntropy Health"


def test_backup_holds_the_database_key_and_uploads(client, isolated_data, tmp_path):
    client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"})
    (isolated_data / "uploads").mkdir(exist_ok=True)
    (isolated_data / "uploads" / "report.pdf").write_bytes(b"%PDF-1.4")
    from app.core import security
    security.encrypt_json("make sure the key exists")
    out = cli.backup(str(tmp_path / "backups"))
    with zipfile.ZipFile(out) as z:
        assert {"syntropy.db", "secret.key", "uploads/report.pdf"} <= set(z.namelist())


def test_service_files(isolated_data):
    unit = cli._systemd_unit("0.0.0.0", 8123, "syntropy")
    assert "User=syntropy" in unit and "--port 8123" in unit and f"SYNTROPY_DATA_DIR={isolated_data}" in unit
    assert "EnvironmentFile=-/etc/syntropy-health/env" in unit and "WantedBy=multi-user.target" in unit
    user_unit = cli._systemd_unit("127.0.0.1", 8000, None)
    assert "User=" not in user_unit and "WantedBy=default.target" in user_unit
    plist = cli._launchd_plist("0.0.0.0", 8000)
    assert cli.LAUNCHD_LABEL in plist and "<string>serve</string>" in plist and str(isolated_data) in plist


def test_mcp_command_for_agents_is_limited_to_one_person(isolated_data):
    cmd = mcp_server.stdio_command("prf_1", "agent:claude")
    assert cmd["args"] == ["-m", "app.mcp_server"] and cmd["env"]["SYNTROPY_MCP_PROFILE"] == "prf_1"
    assert cmd["env"]["SYNTROPY_DATA_DIR"] == str(isolated_data) and Path(cmd["env"]["PYTHONPATH"]) == config.REPO_ROOT


def test_pairing_gives_the_phone_a_network_address(client, monkeypatch):
    monkeypatch.setattr(network, "home_addresses", lambda: ["192.168.1.50"])
    code = client.post("/api/devices/pairing-code", json={}, headers={"host": "127.0.0.1:8001"}).json()
    assert code["server_url"] == "http://127.0.0.1:8001"             # only listening on this computer
    monkeypatch.setenv("SYNTROPY_LISTEN_HOST", "0.0.0.0")
    code = client.post("/api/devices/pairing-code", json={}, headers={"host": "127.0.0.1:8001"}).json()
    assert code["server_url"] == "http://192.168.1.50:8001"
    code = client.post("/api/devices/pairing-code", json={}, headers={"host": "health.example.ts.net"}).json()
    assert code["server_url"] == "http://health.example.ts.net"      # a real address is left alone


def test_cli_version(capsys):
    cli.main(["version"])
    assert capsys.readouterr().out.strip() == config.APP_VERSION

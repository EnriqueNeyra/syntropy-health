"""Starting Windows PowerShell from the app (app/core/windows.py)."""

from app.core import windows


def test_powershell_7s_module_folders_are_left_behind(monkeypatch):
    # Inherited from PowerShell 7, they make Windows PowerShell load 7's Security module, fail, and read no signature.
    monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")
    monkeypatch.setenv("SYNTROPY_SOMETHING", "kept")
    env = windows.powershell_env({"SYNTROPY_SIGNED_FILE": "C:/x.exe"})
    assert not any(k.upper() == "PSMODULEPATH" for k in env)
    assert env["SYNTROPY_SOMETHING"] == "kept" and env["SYNTROPY_SIGNED_FILE"] == "C:/x.exe"

"""Starting Windows PowerShell (powershell.exe, version 5.1) from the app."""

from __future__ import annotations

import os
from typing import Optional


def powershell_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The environment for powershell.exe, without PSModulePath. Started from PowerShell 7, the app inherits 7's module
    folders, and Windows PowerShell then finds 7's own Microsoft.PowerShell.Security and CimCmdlets first, can't load them,
    and Get-AuthenticodeSignature or Get-CimInstance answers nothing. Without the variable it uses its own folders."""
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    env.update(extra or {})
    return env

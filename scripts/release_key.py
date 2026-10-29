"""
Builds the relay key into a release: writes app/core/release_key.py, which config.relay_key() reads.

The release workflows run this before they build the wheel (desktop.yml), the Mac and Windows apps (apps.yml) and the
published Docker images (docker.yml), with the key in SYNTROPY_RELAY_KEY, the repository's Actions secret of that
name. The key isn't in the source code, so a build from it, such as a fork's, has none: Syntropy's sign-in relay and
callback page don't serve it, and it doesn't offer Syntropy's registrations. In the Syntropy Health repository itself a
release without the key would lose them, so there a missing key fails the build.

    SYNTROPY_RELAY_KEY=... python scripts/release_key.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "app" / "core" / "release_key.py"
UPSTREAM = "EnriqueNeyra/syntropy-health"


def write(key: str, target: Path) -> None:
    target.write_text('"""The relay key of a release build, written by scripts/release_key.py. It isn\'t in the source code."""\n\n'
                      f"KEY = {key!r}\n", encoding="utf-8")


def main() -> int:
    key = os.environ.get("SYNTROPY_RELAY_KEY", "").strip()
    if key:
        write(key, TARGET)
        print(f"Built the relay key into {TARGET.relative_to(TARGET.parent.parent.parent)}.")
        return 0
    if os.environ.get("GITHUB_REPOSITORY") == UPSTREAM:
        print("::error::SYNTROPY_RELAY_KEY isn't set, so this build wouldn't carry Syntropy's sign-in registrations. "
              "Add it under the repository's Settings → Secrets and variables → Actions.")
        return 1
    print("No relay key: this build uses its own registrations (Settings → Developer and Settings → Wearables).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

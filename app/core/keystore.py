"""
Where this computer keeps the master key, the one secret that opens the database and everything saved in it.

A file beside the database protects little: anyone with a copy of the folder has both. So, where the system offers
somewhere better, the key lives there:

* **Mac:** the login Keychain, through Apple's ``security`` tool rather than the Keychain API. An item made through the API
  answers only to the program that made it, and asks in a dialog when that program changes: at each update, and whenever
  the MCP server or the command line runs from somewhere else. ``security`` never changes, so nothing ever asks, including
  a background service with nobody there to answer. A program running as this person could ask it too, and it can read the
  server's memory anyway.
* **Windows:** protected with DPAPI, which ties it to this Windows account on this PC, and kept in ``key.json``.
* **Linux and Docker:** ``secret.key`` in the data folder, unless someone says otherwise with ``SYNTROPY_KEY_FILE`` or
  ``SYNTROPY_SECRET_KEY``. There's usually no keystore on a server, and a service can't ask anyone for a password.

``key.json`` in the data folder says where the key is. It isn't secret: without it, a key that's missing from the store
would look like a new installation, and a new key would be made in place of the one the data needs.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core import config

log = logging.getLogger("syntropy.keystore")

INFO_FILE = "key.json"
KEY_FILE = "secret.key"
KEY_BYTES = 32

KEYCHAIN = "keychain"
DPAPI = "dpapi"
FILE = "file"
PROTECTED_STORES = (KEYCHAIN, DPAPI)

SECURITY_TOOL = "/usr/bin/security"
KEYCHAIN_SERVICE = "Syntropy Health encryption key"
_KEYCHAIN_NOT_FOUND = 44        # errSecItemNotFound, as the tool's exit status
_KEYCHAIN_WAIT_S = 30           # it can show a dialog for a locked keychain, with nobody there to answer

# About a quarter of a second and 128 MB for each guess. The same as sign-in passwords (app.core.security).
WRAP_N, WRAP_R, WRAP_P = 2**17, 8, 1
_WRAP_LABEL = b"syntropy-key-recovery"


class Unreachable(Exception):
    """The store is there but can't answer now, such as a locked keychain. Not the same as the key being gone: no new key
    is made because of this."""


class CantSave(Exception):
    """The store won't take the key. The caller falls back to the file."""


def _data_dir() -> Path:
    return config.data_dir()


def _info_path() -> Path:
    return _data_dir() / INFO_FILE


def _key_path() -> Path:
    return _data_dir() / KEY_FILE


def wanted_store() -> str:
    """Where a new key goes. ``SYNTROPY_KEY_STORE=file`` keeps it beside the data even on a Mac or Windows PC."""
    chosen = config.env("SYNTROPY_KEY_STORE").lower()
    if chosen in (KEYCHAIN, DPAPI, FILE):
        return chosen
    return {"darwin": KEYCHAIN, "win32": DPAPI}.get(sys.platform, FILE)


def _read_info() -> dict[str, Any]:
    try:
        info = json.loads(_info_path().read_text(encoding="utf-8"))
        return info if isinstance(info, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Couldn't read %s: %s", _info_path(), exc)
        return {}


def _write_info(info: dict[str, Any]) -> None:
    """Whole or not at all, and private from the moment it exists."""
    path = _info_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(info, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The Keychain
# ---------------------------------------------------------------------------

def _keychain_args() -> list[str]:
    """A keychain of its own for tests: the real one is never touched by them."""
    chosen = config.env("SYNTROPY_KEYCHAIN_FILE")
    return [chosen] if chosen else []


def _keychain_read(account: Optional[str]) -> Optional[bytes]:
    """None when there's no such item. Without an account, whichever this service finds first, for when key.json is gone."""
    who = ["-a", account] if account else []
    try:
        done = subprocess.run([SECURITY_TOOL, "find-generic-password", "-s", KEYCHAIN_SERVICE, *who, "-w", *_keychain_args()],
                              capture_output=True, text=True, timeout=_KEYCHAIN_WAIT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unreachable(f"The Keychain didn't answer: {exc}") from exc
    if done.returncode == _KEYCHAIN_NOT_FOUND:
        return None
    if done.returncode:
        raise Unreachable(f"The Keychain didn't answer: {done.stderr.strip()[:200]}")
    return decode_key(done.stdout.encode())


def _keychain_write(account: str, key: bytes) -> None:
    """Through the tool's standard input, since anything on a command line can be seen by every program on the computer."""
    value = base64.urlsafe_b64encode(key).decode()
    keychain = " ".join(f'"{k}"' for k in _keychain_args())
    command = (f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a "{account}" -l "{KEYCHAIN_SERVICE}" '
               f'-j "Opens this computer\'s Syntropy Health data. Deleting it can make that data unreadable." '
               f'-T "{SECURITY_TOOL}" -w "{value}" {keychain}\n')
    try:
        done = subprocess.run([SECURITY_TOOL, "-i"], input=command, capture_output=True, text=True, timeout=_KEYCHAIN_WAIT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CantSave(f"The Keychain didn't answer: {exc}") from exc
    if done.returncode:
        raise CantSave(f"The Keychain didn't take it: {done.stderr.strip()[:200]}")


def _keychain_delete(account: str) -> None:
    try:
        subprocess.run([SECURITY_TOOL, "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, *_keychain_args()],
                       capture_output=True, timeout=_KEYCHAIN_WAIT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Couldn't remove the key from the Keychain: %s", exc)


# ---------------------------------------------------------------------------
# DPAPI
# ---------------------------------------------------------------------------

def _dpapi(data: bytes, protect: bool) -> bytes:
    """No dependency: crypt32 is part of every Windows. The description names the app for anyone looking."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32   # type: ignore[attr-defined]
    buffer = ctypes.create_string_buffer(data, len(data))
    incoming = Blob(len(data), buffer)
    outgoing = Blob()
    ui_forbidden = 0x01     # CRYPTPROTECT_UI_FORBIDDEN: a service has nobody to answer a dialog
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    description = "Syntropy Health encryption key" if protect else None
    if not call(ctypes.byref(incoming), description, None, None, None, ui_forbidden, ctypes.byref(outgoing)):
        raise OSError(ctypes.get_last_error() or kernel32.GetLastError(), "DPAPI refused")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree(outgoing.pbData)


def _dpapi_save(info: dict[str, Any], key: bytes) -> None:
    try:
        info["dpapi"] = base64.b64encode(_dpapi(key, protect=True)).decode()
    except (OSError, ValueError, AttributeError) as exc:
        raise CantSave(f"Windows' protected storage didn't take it: {exc}") from exc


def _dpapi_read(info: dict[str, Any]) -> Optional[bytes]:
    try:
        return _dpapi(base64.b64decode(info["dpapi"]), protect=False)
    except (KeyError, ValueError):
        return None
    except (OSError, AttributeError) as exc:
        # The blob belongs to another account or computer, or the password was reset by an administrator.
        log.warning("Windows' protected storage couldn't open the key: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Reading and saving
# ---------------------------------------------------------------------------

def decode_key(raw: bytes) -> Optional[bytes]:
    """A key as it's written in a file: base64, in either alphabet, so `openssl rand -base64 32` makes one."""
    text = raw.strip()
    try:
        key = base64.urlsafe_b64decode(text.replace(b"+", b"-").replace(b"/", b"_") + b"=" * (-len(text) % 4))
    except ValueError:
        return None
    return key if len(key) == KEY_BYTES else None


def explicit_file() -> Optional[Path]:
    chosen = config.env("SYNTROPY_KEY_FILE")
    return Path(chosen).expanduser() if chosen else None


def from_store() -> Optional[bytes]:
    """The key in the system's store, if that's where key.json says it is. Raises Unreachable if the store can't answer."""
    info = _read_info()
    store = info.get("store")
    if store == DPAPI:
        return _dpapi_read(info)
    if store == KEYCHAIN:
        return _keychain_read(info.get("account"))
    # Without key.json (removed by hand, or lost with a partial copy) the Keychain may still hold it. The caller checks
    # that it opens this database.
    if not info and wanted_store() == KEYCHAIN:
        return _keychain_read(None)
    return None


def store_of_record() -> Optional[str]:
    return _read_info().get("store")


def save_to_store(key: bytes) -> str:
    """Into the system's store, read back before anything relies on it. Returns which store, ``file`` meaning this system
    has none (or it refused) and the caller keeps the file. What was saved for recovery opened the old key, so it goes."""
    wanted = wanted_store()
    if wanted == FILE:
        return FILE
    info = _read_info()
    try:
        if wanted == KEYCHAIN:
            # A new item each time: an old one left behind is harmless, and it may be what opens something set aside.
            account = uuid.uuid4().hex
            _keychain_write(account, key)
            if _keychain_read(account) != key:
                raise CantSave("The Keychain didn't give the key back as it was saved.")
            info.update(account=account)
        else:
            _dpapi_save(info, key)
            if _dpapi_read(info) != key:
                raise CantSave("Windows' protected storage didn't give the key back as it was saved.")
    except (CantSave, Unreachable) as exc:
        log.warning("Keeping the encryption key in a file beside the data: %s", exc)
        return FILE
    info.update(version=1, store=wanted)
    info.pop("recovery", None)
    _write_info(info)
    return wanted


def forget() -> None:
    """When the data is being deleted for good, the key goes too."""
    info = _read_info()
    if info.get("store") == KEYCHAIN and info.get("account"):
        _keychain_delete(info["account"])
    _info_path().unlink(missing_ok=True)


@contextmanager
def exclusive() -> Iterator[None]:
    """One process at a time, for making the first key: two making one each, and the database opened with the one that
    lost, would be a database nobody can read. The system lets go of the lock if the process dies."""
    path = _data_dir() / "key.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            import time
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)        # lets go of the lock too


# ---------------------------------------------------------------------------
# Locked with a password, for when the store is lost
# ---------------------------------------------------------------------------

Kdf = Callable[[str, bytes, int, int, int], bytes]


def wrap(key: bytes, password: str, kdf: Kdf) -> dict[str, Any]:
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    sealed = AESGCM(kdf(password, salt, WRAP_N, WRAP_R, WRAP_P)).encrypt(nonce, key, _WRAP_LABEL)
    return {"kdf": "scrypt", "n": WRAP_N, "r": WRAP_R, "p": WRAP_P, "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(), "sealed": base64.b64encode(sealed).decode()}


def unwrap(wrapped: dict[str, Any], password: str, kdf: Kdf) -> Optional[bytes]:
    try:
        derived = kdf(password, base64.b64decode(wrapped["salt"]), int(wrapped["n"]), int(wrapped["r"]), int(wrapped["p"]))
        return AESGCM(derived).decrypt(base64.b64decode(wrapped["nonce"]), base64.b64decode(wrapped["sealed"]), _WRAP_LABEL)
    except (InvalidTag, KeyError, ValueError, TypeError, MemoryError):
        return None


def recovery_entries() -> dict[str, dict[str, Any]]:
    entries = _read_info().get("recovery")
    return entries if isinstance(entries, dict) else {}


def recovery_turned_off() -> bool:
    return bool(_read_info().get("recovery_off"))


def set_recovery_off(off: bool) -> None:
    info = _read_info()
    info["recovery_off"] = off
    if off:
        info.pop("recovery", None)
    _write_info(info)


def save_recovery(account_id: str, wrapped: dict[str, Any]) -> None:
    info = _read_info()
    info.setdefault("recovery", {})[account_id] = wrapped
    _write_info(info)


def drop_recovery(account_id: str) -> None:
    info = _read_info()
    if account_id in (info.get("recovery") or {}):
        del info["recovery"][account_id]
        _write_info(info)


def retired_blob() -> Optional[str]:
    return _read_info().get("retired")


def save_retired_blob(sealed: Optional[str]) -> None:
    info = _read_info()
    if sealed:
        info["retired"] = sealed
    else:
        info.pop("retired", None)
    _write_info(info)

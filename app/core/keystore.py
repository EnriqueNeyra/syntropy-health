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
import re
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


def _keychain_tool(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([SECURITY_TOOL, *args, *_keychain_args()], capture_output=True, text=True, errors="replace",
                              timeout=_KEYCHAIN_WAIT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise Unreachable("The Keychain didn't answer. It may be locked: unlock this Mac, then start Syntropy Health again.") from exc
    except OSError as exc:
        raise Unreachable(f"The Keychain didn't answer: {exc}") from exc


def _keychain_read(account: Optional[str]) -> Optional[bytes]:
    """None when there's no such item. Without an account, whichever this service finds first."""
    who = ["-a", account] if account else []
    done = _keychain_tool(["find-generic-password", "-s", KEYCHAIN_SERVICE, *who, "-w"])
    if done.returncode == _KEYCHAIN_NOT_FOUND:
        return None
    if done.returncode:
        raise Unreachable(f"The Keychain didn't answer: {done.stderr.strip()[:200]}")
    return decode_key(done.stdout.encode())


def _folder_tag() -> Optional[str]:
    """Which data folder an item is for, kept with it (as its "generic attribute") so that removing this folder's key removes
    every one it ever had, and no other folder's: the desktop app, a server installed by script and a copy of the code each
    have their own items under the one service name. None for a path the tool's input can't carry."""
    folder = str(_data_dir().resolve())
    return None if any(c in folder for c in '"\\') or not folder.isprintable() else folder


_ATTRIBUTE = re.compile(r'^\s*"(acct|svce|gena)"<blob>=(?:"(.*)"|0x([0-9A-Fa-f]+)\b.*)$')


def _keychain_items() -> list[dict[str, str]]:
    """Every item of this service, by account and folder (see _folder_tag), from a listing of what the keychains hold. The
    listing shows names, never the secrets, and asks nothing."""
    done = _keychain_tool(["dump-keychain"])
    if done.returncode:
        raise Unreachable(f"The Keychain didn't answer: {done.stderr.strip()[:200]}")
    items: list[dict[str, str]] = []
    for line in done.stdout.splitlines():
        if line.startswith("keychain:"):
            items.append({})
        elif items and (found := _ATTRIBUTE.match(line)):
            name, text, hexed = found.groups()
            try:
                items[-1][name] = text if text is not None else bytes.fromhex(hexed).decode()
            except (ValueError, UnicodeDecodeError):
                pass
    return [i for i in items if i.get("svce") == KEYCHAIN_SERVICE and i.get("acct")]


def _keychain_write(account: str, key: bytes) -> None:
    """Through the tool's standard input, since anything on a command line can be seen by every program on the computer."""
    value = base64.urlsafe_b64encode(key).decode()
    keychain = " ".join(f'"{k}"' for k in _keychain_args())
    folder = _folder_tag()
    tag = f'-G "{folder}" ' if folder else ""
    command = (f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a "{account}" -l "{KEYCHAIN_SERVICE}" {tag}'
               f'-j "Opens this computer\'s Syntropy Health data. Deleting it can make that data unreadable." '
               f'-T "{SECURITY_TOOL}" -w "{value}" {keychain}\n')
    try:
        done = subprocess.run([SECURITY_TOOL, "-i"], input=command, capture_output=True, text=True, timeout=_KEYCHAIN_WAIT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CantSave(f"The Keychain didn't answer: {exc}") from exc
    if done.returncode:
        raise CantSave(f"The Keychain didn't take it: {done.stderr.strip()[:200]}")


def _keychain_delete(match: list[str]) -> bool:
    """One item matching, whichever is found first. Whether one was there to delete."""
    try:
        return not _keychain_tool(["delete-generic-password", "-s", KEYCHAIN_SERVICE, *match]).returncode
    except Unreachable as exc:
        log.warning("Couldn't remove the key from the Keychain: %s", exc)
        return False


def _keychain_delete_all(match: list[str]) -> None:
    for _ in range(1000):       # however many there are, but never for ever
        if not _keychain_delete(match):
            return


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
    """The key in the system's store, if that's where key.json says it is. Raises Unreachable if the store can't answer.

    Only that one. The Keychain can hold others, left by a data folder deleted by hand or by an uninstall that kept them, and
    a new database given one of those would have a key that nothing here names: backups would leave it out, and nothing could
    open them anywhere else."""
    info = _read_info()
    store = info.get("store")
    if store == DPAPI:
        return _dpapi_read(info)
    if store == KEYCHAIN:
        return _keychain_read(info.get("account"))
    return None


def unrecorded() -> Iterator[tuple[str, bytes]]:
    """Without key.json (removed by hand, or lost with a partial copy), the keys the Keychain holds for this app, by account:
    one may be what opens the data here. This folder's first. Only for data that's already there, which the caller tries each
    against, then records the one that opens it (``record``). Raises Unreachable."""
    if _read_info() or wanted_store() != KEYCHAIN:
        return
    folder = _folder_tag()
    items = sorted(_keychain_items(), key=lambda i: i.get("gena") != folder)
    for account in dict.fromkeys(i["acct"] for i in items):
        if key := _keychain_read(account):
            yield account, key


def record(account: str) -> None:
    """key.json again, for a key found in the Keychain without it."""
    info = _read_info()
    info.update(version=1, store=KEYCHAIN, account=account)
    _write_info(info)


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
            if info.get("store") == KEYCHAIN and info.get("account") and info["account"] != account:
                # So that forgetting this folder's key finds the ones it had before, including those saved without a folder.
                info["earlier_accounts"] = list(dict.fromkeys([*info.get("earlier_accounts", []), info["account"]]))[-50:]
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
    """When the data is being deleted for good, the key goes too: every one this folder has had in the Keychain, including
    those of a folder here before it that was deleted by hand. Other folders' are left alone."""
    info = _read_info()
    if info.get("store") == KEYCHAIN or wanted_store() == KEYCHAIN:
        for account in dict.fromkeys([info.get("account"), *(info.get("earlier_accounts") or [])]):
            if isinstance(account, str) and account:
                _keychain_delete(["-a", account])
        if folder := _folder_tag():
            _keychain_delete_all(["-G", folder])
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

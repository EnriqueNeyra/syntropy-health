"""
Cryptographic primitives.

* The master key, which comes from ``SYNTROPY_SECRET_KEY``, ``SYNTROPY_KEY_FILE``, the system's key store or
  ``<data_dir>/secret.key`` (see app.core.keystore). Two keys are derived from it: one encrypts the database
  (app.core.db), the other the fields below.
* Field-level encryption (Fernet / AES-128-CBC + HMAC-SHA256) for OAuth tokens and
  other secrets stored in SQLite. They stay encrypted in exports of the database and in a backup without a passphrase.
* scrypt hashing for the local password.
* Opaque random tokens (sessions, device tokens, pairing codes) stored only as hashes.
* HMAC-signed compact tokens (used by the built-in EHR simulator).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core import config, db, keystore
from app.core.sqlite import sqlite3

_key_lock = threading.RLock()
_key_cache: dict[str, bytes] = {}
_KEY_CHECK_SETTING = "security.key_check"
_KEY_CHECK_VALUE = "syntropy-key-check"
_key_mismatch: dict[str, str] = {}      # by database


def _key_file() -> Path:
    return config.data_dir() / keystore.KEY_FILE


def master_key() -> bytes:
    """Returns the 32-byte master key (raw). Created on first use, but never in place of one that saved secrets or an
    encrypted database need: that would make them look like they were never there."""
    env_key = config.env("SYNTROPY_SECRET_KEY")
    if env_key:
        return key_from_setting(env_key)

    chosen = keystore.explicit_file()
    path = chosen or _key_file()
    cache_key = str(path)
    if cache_key in _key_cache:
        return _key_cache[cache_key]
    with _key_lock:
        if cache_key in _key_cache:
            return _key_cache[cache_key]
        key = _read_chosen_file(chosen) if chosen else _find_key(path)
        _key_cache[cache_key] = key
        return key


_KEY_ADVICE = ("Put the file back, or take it from a backup with `syntropy-health restore BACKUP-FILE --key-only`, and start "
               "Syntropy Health again. If there's no backup, run "
               "`syntropy-health key new`: your records are safe, everyone then sets a new password with "
               "`syntropy-health accounts reset-password NAME`, and health systems and AI services need signing in to again.")
# An encrypted database has no such way out, and nothing here should suggest one.
ENCRYPTED_KEY_ADVICE = ("To get it back: enter your Syntropy Health password if you're asked for it, or run "
                        "`syntropy-health key recover`; or take the key from any backup made with this version with "
                        "`syntropy-health restore BACKUP-FILE --key-only`; or put back a copy you kept, as secret.key in the data "
                        "folder. On another computer, restore a backup instead. Without any of those your records can't be "
                        "read by anyone. Nothing has been changed.")


def _read_chosen_file(path: Path) -> bytes:
    """SYNTROPY_KEY_FILE: a file someone chose, such as a Docker secret. Anything other than a key in it is treated as
    SYNTROPY_SECRET_KEY treats its value, so `openssl rand -hex 32 > file` works too. Made if it isn't there, unless
    something already needs the key it would have held."""
    try:
        raw = path.read_bytes().strip()
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        raise db.StartupProblem("key_locked", f"The encryption key file {path} (SYNTROPY_KEY_FILE) can't be read: {exc}.") from exc
    if raw:
        return keystore.decode_key(raw) or hashlib.sha256(raw).digest()
    if db.is_encrypted(config.db_path()) or _stored_secrets():
        raise db.StartupProblem("key", f"The encryption key file {path} (SYNTROPY_KEY_FILE) is missing or empty. {ENCRYPTED_KEY_ADVICE}")
    try:
        return _write_new_key(path, replace=path.exists())
    except OSError as exc:
        raise db.StartupProblem("key_locked", f"The encryption key file {path} (SYNTROPY_KEY_FILE) can't be created: {exc}.") from exc


def _find_key(path: Path) -> bytes:
    """A secret.key in the data folder comes first, since someone putting one back is the advice given when a key is lost.
    With an encrypted database, whichever key opens it is the key, whatever else is lying around. Only with nothing here
    needing a key is a new one made."""
    damaged: Optional[db.StartupProblem] = None
    in_file = None
    try:
        in_file = _read_key(path) if path.exists() else None
    except db.StartupProblem as exc:
        damaged = exc
    unreachable: Optional[keystore.Unreachable] = None
    try:
        in_store = keystore.from_store()
    except keystore.Unreachable as exc:
        in_store, unreachable = None, exc
    found = [k for k in (in_file, in_store) if k]
    database = config.db_path()
    if db.is_encrypted(database):
        for key in found:
            for candidate in (key, *_retired_keys(key)):
                if db.opens(database, database_key(candidate)):
                    return candidate
        if not unreachable and (left := _left_in_store(database)):
            return left
        if unreachable:
            raise db.StartupProblem("key_locked", locked_text(unreachable))
        if damaged:
            raise damaged
        raise db.StartupProblem("key", f"Your records are encrypted, and the key that opens them isn't on this computer, or the "
                                       f"database file is damaged. {ENCRYPTED_KEY_ADVICE}")
    if found:
        return found[0]
    if unreachable:
        raise db.StartupProblem("key_locked", locked_text(unreachable))
    if damaged:
        raise damaged
    if _stored_secrets():
        if left := _left_in_store(database):
            return left
        raise db.StartupProblem("key", f"The encryption key file is missing ({path}). Saved passwords, sign-ins and AI keys "
                                       f"can't be read without it, so a new one hasn't been made. {_KEY_ADVICE}")
    # Nothing here needs a key, so it gets a new one, recorded in key.json: never one the Keychain still holds from data
    # deleted before, which nothing here would name, and which backups would then leave out.
    return _new_key(path)


def _left_in_store(database: Path) -> Optional[bytes]:
    """key.json is gone, but the data is here: of the keys the Keychain still holds for this app, the one that opens it. It's
    recorded in key.json again, so backups carry it and forgetting this folder's key removes it. The store not answering is
    a locked key, not a lost one."""
    try:
        for account, key in keystore.unrecorded():
            if opens(database, key):
                keystore.record(account)
                return key
    except keystore.Unreachable as exc:
        raise db.StartupProblem("key_locked", locked_text(exc)) from exc
    return None


def locked_text(exc: Exception) -> str:
    return (f"Syntropy Health can't reach its encryption key right now: {exc}. Unlock your keychain, or sign in to this computer "
            "(rather than connecting from elsewhere), and try again. Nothing has been changed.")


def _new_key(path: Path) -> bytes:
    with keystore.exclusive():
        # Another process may have made it while this one waited.
        if path.exists():
            return _read_key(path)
        try:
            if existing := keystore.from_store():
                return existing
        except keystore.Unreachable:
            pass
        key = secrets.token_bytes(32)
        if keystore.save_to_store(key) == keystore.FILE:
            return _write_new_key(path, key=key)
        return key


def _read_key(path: Path) -> bytes:
    raw = path.read_bytes().strip()
    if not raw and not db.is_encrypted(config.db_path()) and not _stored_secrets():      # a crash on the very first run, before anything was encrypted
        return _write_new_key(path, replace=True)
    try:
        key = base64.urlsafe_b64decode(raw)
    except ValueError:
        key = b""
    if len(key) != 32:
        advice = ENCRYPTED_KEY_ADVICE if db.is_encrypted(config.db_path()) else _KEY_ADVICE
        raise db.StartupProblem("key", f"The encryption key file {path} is damaged: it should hold a 32-byte key. {advice}")
    return key


def _write_new_key(path: Path, replace: bool = False, key: Optional[bytes] = None) -> bytes:
    """Whole or not at all: a crash can't leave an empty file, and if another process got there first, its key is the key."""
    key = key or secrets.token_bytes(32)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.urlsafe_b64encode(key))
        fh.flush()
        os.fsync(fh.fileno())
    db.same_owner(tmp, path.parent)
    try:
        if replace:
            os.replace(tmp, path)
            return key
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _read_key(path)
        except OSError:     # a file system without hard links
            if path.exists():
                return _read_key(path)
            os.replace(tmp, path)
        return key
    finally:
        tmp.unlink(missing_ok=True)


def _secrets_in(conn: Any) -> list[str]:
    rows = [r[0] for r in conn.execute(
        "SELECT value FROM app_settings WHERE key LIKE 'ai.key.%' OR key LIKE '%.client_secret' OR key = 'auth.password_hash' LIMIT 20")]
    try:
        rows += [r[0] for r in conn.execute("SELECT password_hash FROM accounts WHERE password_hash IS NOT NULL LIMIT 20")]
    except sqlite3.OperationalError:     # a backup from before accounts existed
        pass
    out = []
    for value in rows:
        try:
            out.append(json.loads(value))
        except ValueError:
            pass
    out += [r[0] for r in conn.execute("SELECT credentials_enc FROM connections WHERE credentials_enc IS NOT NULL LIMIT 20")]
    return [t for t in out if isinstance(t, str) and t]


def _stored_secrets() -> list[str]:
    """A few of what's encrypted in the database, whichever kinds there are. Empty for a database that's new. One that's
    encrypted can't be asked here: the question only comes up without a key, and master_key answers it first."""
    path = config.db_path()
    if not path.exists() or not path.stat().st_size or db.is_encrypted(path):
        return []
    with db.read() as conn:
        return _secrets_in(conn)


def key_from_file(raw: bytes) -> Optional[bytes]:
    """The key in a secret.key file's bytes, or None if they aren't one."""
    return keystore.decode_key(raw)


def key_as_file(key: bytes) -> bytes:
    return base64.urlsafe_b64encode(key)


def key_from_setting(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def database_key(key: Optional[bytes] = None) -> str:
    """What SQLCipher is given for this master key: the raw form, so no slow key stretching at every connection (the app
    opens one per write), which the key doesn't need, being 256 random bits rather than something a person chose."""
    derived = hmac.new(key or master_key(), b"database-encryption", hashlib.sha256).hexdigest()
    return f"x'{derived}'"


def _fernet_for(key: bytes) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hmac.new(key, b"field-encryption", hashlib.sha256).digest()))


# ---------------------------------------------------------------------------
# Keys this computer has had
# ---------------------------------------------------------------------------

def _retired_keys(key: bytes) -> list[bytes]:
    """Keys replaced when a backup brought its own. Copies kept from before (pre-restore/) still need them, and a crash
    partway through a restore can leave the old database with the new key. Kept encrypted with the current key, so the
    list is no more readable than the data."""
    return _earlier_keys(key) or []


def _earlier_keys(key: bytes) -> Optional[list[bytes]]:
    """As _retired_keys, but None when there's a list this key can't read."""
    sealed = keystore.retired_blob()
    if not sealed:
        return []
    try:
        return [base64.b64decode(k) for k in json.loads(_fernet_for(key).decrypt(sealed.encode()))]
    except (InvalidToken, ValueError):
        return None


def known_database_keys() -> list[str]:
    """Every key something kept here might be encrypted with, the current one first. None if this computer has none yet, and
    it doesn't make one: this is only for looking."""
    key = existing_key()
    return [database_key(k) for k in (key, *_retired_keys(key))] if key else []


def explicit_source() -> Optional[str]:
    """How someone has chosen the key themselves: such a key isn't ours to replace."""
    if config.env("SYNTROPY_SECRET_KEY"):
        return "setting"
    return "key-file" if keystore.explicit_file() else None


def existing_key(*, strict: bool = False) -> Optional[bytes]:
    """The key this computer has now, if any, without making one. For looking, not relying on: it doesn't check that the key
    opens anything. ``strict``: a store that can't answer raises keystore.Unreachable, rather than looking like no key, for
    what's about to replace it."""
    if config.env("SYNTROPY_SECRET_KEY") or keystore.explicit_file():
        try:
            return master_key()
        except db.StartupProblem:
            return None
    try:
        path = _key_file()
        if path.exists() and (in_file := key_from_file(path.read_bytes())):
            return in_file
        return keystore.from_store()
    except OSError:
        return None
    except keystore.Unreachable:
        if strict:
            raise
        return None


def key_in_use() -> bool:
    """Whether what's here relies on a key, wherever it's kept, without making one: the one this process is using, one this
    computer has, or the one an encrypted database needs."""
    with _key_lock:
        if _key_cache.get(str(keystore.explicit_file() or _key_file())):
            return True
    return existing_key() is not None or db.is_encrypted(config.db_path())


def adopt_key(new: bytes, *, keep_replaced: bool = True) -> None:
    """A key from a backup, or unlocked with a password, becomes this computer's. Saved before anything relies on it, and
    read back. The one it replaces is kept, encrypted with the new one (see _retired_keys). Raises keystore.Unreachable,
    having changed nothing, if the store can't say what the key is now: the keys kept from before can't be carried over
    without it."""
    if len(new) != keystore.KEY_BYTES:
        raise ValueError("A key is 32 bytes.")
    path = _key_file()
    with _key_lock:
        old = existing_key(strict=True)
        # A key brought back with a password is the one the list was saved with, though the store has lost it.
        earlier = _earlier_keys(old or new)
        keep = [k for k in dict.fromkeys(([old] if old else []) + (earlier or [])) if k != new]
        store = keystore.save_to_store(new)
        if store == keystore.FILE:
            if keep_replaced and path.exists() and key_from_file(path.read_bytes()) not in (new, None):
                os.replace(path, path.with_name(f"{path.name}.replaced-{time.strftime('%Y%m%d-%H%M%S')}"))
            _write_new_key(path, replace=True, key=new)
        else:
            path.unlink(missing_ok=True)
        if keep or earlier is not None:     # never an empty list in place of one that couldn't be read
            keystore.save_retired_blob(_fernet_for(new).encrypt(json.dumps([base64.b64encode(k).decode() for k in keep]).encode()).decode()
                                       if keep else None)
        _key_cache[str(path)] = new
    _key_mismatch.pop(str(config.db_path()), None)


def has_secrets(database: Path) -> bool:
    """For a backup being restored: is anything encrypted in that file? One that's encrypted has to be opened by the caller first."""
    conn = sqlite3.connect(database)
    try:
        return bool(_secrets_in(conn))
    finally:
        conn.close()


def opens(database: Path, key: bytes) -> bool:
    """For a backup being restored: does this key open what's saved in that file? Decided as check_key decides it. For an
    encrypted database that's simple: the same key opens the file and its fields."""
    if db.is_encrypted(database):
        return db.opens(database, database_key(key))
    conn = sqlite3.connect(database)
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (_KEY_CHECK_SETTING,)).fetchone()
        stored = _secrets_in(conn)
    finally:
        conn.close()
    fernet = _fernet_for(key)

    def readable(token: str) -> bool:
        try:
            fernet.decrypt(token.encode())
            return True
        except (InvalidToken, ValueError):
            return False
    marker = json.loads(row[0]) if row else None
    if marker and readable(marker):
        return True
    return bool(stored) and (all if marker else any)(readable(t) for t in stored)


def forget_key() -> None:
    """The database and key file have just been replaced."""
    with _key_lock:
        _key_cache.clear()
    _key_mismatch.clear()


def _readable(token: str) -> bool:
    try:
        _fernet().decrypt(token.encode())
        return True
    except (InvalidToken, ValueError):
        return False


def check_key() -> None:
    """At start-up: does this key open what's saved? A marker saved with the key answers that, once it's there; before
    that (an upgrade), whatever is already encrypted does. A marker this key can't open isn't the last word: after
    `key new` it belongs to the new key, and the old key put back still opens everything else, which is what counts.
    Wrong, the server still runs, and owners are told."""
    master_key()
    path = str(config.db_path())
    with db.read() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (_KEY_CHECK_SETTING,)).fetchone()
    marker = json.loads(row[0]) if row else None
    marked = bool(marker) and _readable(marker)
    stored = _stored_secrets()
    fine = marked or not stored or (all if marker else any)(_readable(t) for t in stored)
    if fine and not marked:
        _save_key_check()
    if fine:
        _key_mismatch.pop(path, None)
    else:
        where = "the SYNTROPY_SECRET_KEY setting" if config.env("SYNTROPY_SECRET_KEY") else str(_key_file())
        _key_mismatch[path] = (f"Saved passwords, sign-ins to health systems and AI keys can't be read, because the encryption key "
                               f"({where}) isn't the one they were saved with. Records are safe, but sources have stopped "
                               f"syncing. {_KEY_ADVICE}")


def _save_key_check() -> None:
    with db.db() as conn:
        conn.execute("INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (_KEY_CHECK_SETTING, json.dumps(encrypt_json(_KEY_CHECK_VALUE)), time.time()))


def key_problem() -> Optional[str]:
    return _key_mismatch.get(str(config.db_path()))


def start_new_key() -> Optional[Path]:
    """The owner's decision (`syntropy-health key new`) that the old key is gone for good. The old file is kept beside
    the new one, in case it's the right one after all. Returns where it went. A database that's encrypted keeps the key
    that opens it: only the check that saved secrets open starts again, and those that don't stay unreadable."""
    path = _key_file()
    moved = None
    if db.is_encrypted(config.db_path()):
        master_key()        # not opening is a StartupProblem: no new key can help that
    else:
        with _key_lock:
            if path.exists():
                moved = path.with_name(f"{path.name}.replaced-{time.strftime('%Y%m%d-%H%M%S')}")
                os.replace(path, moved)
            key = secrets.token_bytes(32)
            if keystore.save_to_store(key) == keystore.FILE:
                _write_new_key(path, key=key)
            _key_cache[str(path)] = key
    _save_key_check()
    _key_mismatch.pop(str(config.db_path()), None)
    return moved


# ---------------------------------------------------------------------------
# Opening the key with a password, for when the store that held it is lost
# ---------------------------------------------------------------------------

def recovery_applies() -> bool:
    """Only where the key is kept apart from the data. With it beside the data, a copy of the folder already holds it,
    and someone who chose where it lives (a setting, a key file) has chosen how to look after it."""
    return not explicit_source() and keystore.store_of_record() in keystore.PROTECTED_STORES and not keystore.recovery_turned_off()


def _wrap_kdf(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return _scrypt(password, salt, n, r, p)


def note_owner_password(account_id: str, password: str, *, changed: bool = False) -> None:
    """An owner has just proved their password, or set a new one: it's the only time it's known. A new one replaces the
    old one's lock, so an old password can't open a copy from now on. A lock can always be made again at the next sign-in, so
    failing here is only worth a line in the log."""
    if not recovery_applies() or (not changed and account_id in keystore.recovery_entries()):
        return
    try:
        keystore.save_recovery(account_id, keystore.wrap(master_key(), password, _wrap_kdf))
    except (OSError, ValueError, ServerBusy, db.StartupProblem) as exc:
        keystore.log.warning("Couldn't lock the encryption key with the owner's password: %s", exc)


def forget_owner_password(account_id: str) -> None:
    keystore.drop_recovery(account_id)


def _owners_in(database: Path, key: bytes) -> Optional[set[str]]:
    """The owners' accounts in this database, read with this key. None if they can't be read."""
    conn = sqlite3.connect(str(database), timeout=2.0)
    try:
        if db.is_encrypted(database):
            db.use_key(conn, database_key(key))
        return {r[0] for r in conn.execute("SELECT id FROM accounts WHERE role = 'owner'")}
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def forget_former_owners() -> None:
    """At start-up: the locks of anyone who isn't an owner here any more go, whatever left them (an account removed by an
    earlier version, or a backup restored from before an owner joined). Their passwords stop opening the key."""
    entries = keystore.recovery_entries()
    if not entries:
        return
    try:
        with db.read() as conn:
            owners = {r[0] for r in conn.execute("SELECT id FROM accounts WHERE role = 'owner'")}
        for account_id in set(entries) - owners:
            keystore.drop_recovery(account_id)
    except (sqlite3.Error, OSError) as exc:
        keystore.log.warning("Couldn't check whose passwords open the encryption key: %s", exc)


def recover_key(password: str) -> bool:
    """Whether this password opened a lock on the key. If it did, the key opens what's here, and the lock's account is still
    an owner in it, the key is this computer's again. A lock left by someone who has since left or been removed opens
    nothing, and goes."""
    path = config.db_path()
    for account_id, wrapped in keystore.recovery_entries().items():
        key = keystore.unwrap(wrapped, password, _wrap_kdf)
        if not key or (db.is_encrypted(path) and not db.opens(path, database_key(key))):
            continue
        owners = _owners_in(path, key)
        if owners is None:
            keystore.log.warning("Couldn't check that the password's account is still an owner, so it didn't open the key")
            continue
        if account_id not in owners:
            forget_owner_password(account_id)
            continue
        adopt_key(key)
        return True
    return False


def _derive(purpose: str) -> bytes:
    return hmac.new(master_key(), purpose.encode(), hashlib.sha256).digest()


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_derive("field-encryption")))


def encrypt_json(value: Any) -> str:
    return _fernet().encrypt(json.dumps(value).encode()).decode()


def decrypt_json(token: Optional[str]) -> Any:
    if not token:
        return None
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except (InvalidToken, ValueError):
        return None


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

# About a quarter of a second and 128 MB on a laptop, a second or so on a Raspberry Pi. Passwords saved with lower
# settings keep working (each hash names its own) and are upgraded at the next sign-in.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**17, 8, 1
# A small computer that can't spare the memory still gets as much as it can.
_SCRYPT_N_LOWEST = 2**14
# Each check takes that memory, so a burst of sign-ins can't take the server down: the others wait their turn.
_hashing = threading.BoundedSemaphore(2)
_HASHING_WAIT_S = 30


class ServerBusy(Exception):
    pass


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    if not _hashing.acquire(timeout=_HASHING_WAIT_S):
        raise ServerBusy("Too many sign-ins at once. Try again in a moment.")
    try:
        return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=128 * r * (n + p + 2) + 1024 * 1024)
    finally:
        _hashing.release()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n = _SCRYPT_N
    while True:
        try:
            digest = _scrypt(password, salt, n, _SCRYPT_R, _SCRYPT_P)
            break
        except (ValueError, MemoryError):
            if n <= _SCRYPT_N_LOWEST:
                raise
            n //= 2
    return "scrypt${}${}${}${}${}".format(
        n, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        digest = _scrypt(password, base64.b64decode(salt_b64), int(n), int(r), int(p))
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError, MemoryError):
        return False


def password_needs_upgrade(stored: str) -> bool:
    """Whether this hash was made with weaker settings than a new one would be."""
    try:
        algo, n, r, p, *_ = stored.split("$")
        return algo != "scrypt" or (int(n), int(r), int(p)) < (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    except ValueError:
        return False


_decoy: dict[int, str] = {}


def waste_a_password_check(password: str) -> None:
    """For a name that doesn't exist: takes as long as checking a real password, so timing doesn't tell who signs in here."""
    decoy = _decoy.get(_SCRYPT_N)
    if decoy is None:
        decoy = _decoy.setdefault(_SCRYPT_N, hash_password(secrets.token_urlsafe(16)))
    verify_password(password, decoy)


# ---------------------------------------------------------------------------
# Opaque tokens
# ---------------------------------------------------------------------------

def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def pairing_code(length: int = 8) -> str:
    return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(length))


def normalize_pairing_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


# ---------------------------------------------------------------------------
# Signed compact tokens (payload.signature, base64url)
# ---------------------------------------------------------------------------

def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign(payload: dict[str, Any], purpose: str) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(_derive(f"sign:{purpose}"), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def unsign(token: str, purpose: str) -> Optional[dict[str, Any]]:
    """Returns the payload if the signature is valid and ``exp`` (if set) is in the future."""
    try:
        body, sig = token.split(".", 1)
        expected = _b64e(hmac.new(_derive(f"sign:{purpose}"), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64d(body))
        if "exp" in payload and float(payload["exp"]) < time.time():
            return None
        return payload
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# RS384 JWTs (SMART asymmetric client authentication)
# ---------------------------------------------------------------------------

def _int_b64(value: int) -> str:
    return _b64e(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _b64_int(data: str) -> int:
    return int.from_bytes(_b64d(data), "big")


def generate_rsa_signing_key() -> tuple[str, dict[str, str]]:
    """A new RSA-2048 key: (private key PEM, public JWK with a random kid)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    nums = key.public_key().public_numbers()
    jwk = {"kty": "RSA", "alg": "RS384", "use": "sig", "kid": random_token(12),
           "n": _int_b64(nums.n), "e": _int_b64(nums.e)}
    return pem, jwk


def sign_jwt_rs384(claims: dict[str, Any], private_key_pem: str, kid: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    header = {"alg": "RS384", "typ": "JWT", "kid": kid}
    signing_input = f"{_b64e(json.dumps(header, separators=(',', ':')).encode())}.{_b64e(json.dumps(claims, separators=(',', ':')).encode())}"
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA384())  # type: ignore[union-attr]
    return f"{signing_input}.{_b64e(signature)}"


def verify_jwt_rs384(token: str, jwk: dict[str, str]) -> Optional[dict[str, Any]]:
    """Returns the claims if ``token`` is an RS384 JWT signed by ``jwk`` (claims are not checked)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        if json.loads(_b64d(head_b64)).get("alg") != "RS384":
            return None
        public = rsa.RSAPublicNumbers(_b64_int(jwk["e"]), _b64_int(jwk["n"])).public_key()
        public.verify(_b64d(sig_b64), f"{head_b64}.{body_b64}".encode(), padding.PKCS1v15(), hashes.SHA384())
        return json.loads(_b64d(body_b64))
    except (ValueError, TypeError, KeyError, InvalidSignature):
        return None


def pkce_challenge(verifier: str) -> str:
    return _b64e(hashlib.sha256(verifier.encode("ascii")).digest())


def generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    return verifier, pkce_challenge(verifier)

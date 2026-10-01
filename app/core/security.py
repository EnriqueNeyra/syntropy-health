"""
Cryptographic primitives.

* Field-level encryption (Fernet / AES-128-CBC + HMAC-SHA256) for OAuth tokens and
  other secrets stored in SQLite. The key comes from ``SYNTROPY_SECRET_KEY`` or is
  generated once into ``<data_dir>/secret.key`` (mode 0600).
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

from app.core import config

_key_lock = threading.Lock()
_key_cache: dict[str, bytes] = {}


def _key_file() -> Path:
    return config.data_dir() / "secret.key"


def master_key() -> bytes:
    """Returns the 32-byte master key (raw)."""
    env_key = config.env("SYNTROPY_SECRET_KEY")
    if env_key:
        return hashlib.sha256(env_key.encode()).digest()

    path = _key_file()
    cache_key = str(path)
    if cache_key in _key_cache:
        return _key_cache[cache_key]
    with _key_lock:
        if cache_key in _key_cache:
            return _key_cache[cache_key]
        if path.exists():
            key = base64.urlsafe_b64decode(path.read_bytes().strip())
        else:
            key = secrets.token_bytes(32)
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(base64.urlsafe_b64encode(key))
        _key_cache[cache_key] = key
        return key


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

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=32,
        )
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


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

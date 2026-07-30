"""Password hashing, token minting, envelope encryption, key derivation.

Everything security-sensitive that produces or verifies bytes lives here, so
there is exactly one place to review and exactly one place to change when a
parameter needs raising.

Choices and why:

* **Argon2id** for passwords. Memory-hard, so a GPU farm buys far less than it
  does against bcrypt or PBKDF2, and it is the current OWASP first
  recommendation. Parameters follow the OWASP minimum (64 MiB, t=3, p=4).
* **SHA-256 for token digests, not Argon2.** A refresh token or API key is 256
  bits of `secrets.token_urlsafe` entropy — there is nothing to brute-force,
  and a lookup on every request must be fast. Argon2 here would add 50 ms to
  every authenticated call and buy nothing.
* **PyJWT rather than python-jose.** python-jose carries three advisories
  with no fix for the latest, and pulls in `ecdsa`, whose Minerva timing
  vulnerability the maintainers have declared out of scope and will not fix.
  The platform signs HS256 and nothing else, so none of the asymmetric
  machinery either library provides is used — swapping to PyJWT removed four
  unfixable CVEs from the dependency tree at the cost of two import lines.
* **AES-256-GCM for stored secrets.** Authenticated encryption: a tampered
  ciphertext fails to decrypt rather than decrypting to something wrong. Keyed
  by version so keys can be rotated.
* **`secrets.compare_digest` for every comparison** of anything secret. A
  plain `==` on a token leaks its prefix through timing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
#: OWASP-recommended Argon2id parameters: 64 MiB, three iterations, four lanes.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def hash_password(password: str) -> str:
    """Return an Argon2id PHC string. Salt is generated internally."""
    if not password:
        raise ValueError("refusing to hash an empty password")
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Constant-time-ish verification that never raises.

    A null hash — a user who only ever signs in with Google — must return
    False, not crash and not accidentally succeed. The dummy verification on
    the null path keeps the timing of "no such user" close to that of "wrong
    password", so the endpoint does not become a user-enumeration oracle.
    """
    if not stored_hash:
        _dummy_verify()
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


#: Precomputed once so the decoy path costs a verification, not a hash.
_DUMMY_HASH = _hasher.hash("timing-equalisation-placeholder")


def _dummy_verify() -> None:
    try:
        _hasher.verify(_DUMMY_HASH, "wrong")
    except Exception:  # noqa: BLE001 — the point is to burn the time
        pass


def needs_rehash(stored_hash: str) -> bool:
    """True when the stored hash used weaker parameters than current policy.

    Called after a successful sign-in: the plaintext is in hand exactly once,
    which is the only moment a silent upgrade is possible.
    """
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, ValueError):
        return True


# ---------------------------------------------------------------------------
# Opaque tokens
# ---------------------------------------------------------------------------
#: 32 bytes → 43 URL-safe characters. Well beyond any brute-force horizon.
TOKEN_BYTES = 32


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 hex digest. What gets stored; the plaintext never is."""
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    return secrets.compare_digest(a, b)


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------
API_KEY_PREFIX_LIVE = "ierp_live"
API_KEY_PREFIX_TEST = "ierp_test"


@dataclass(frozen=True, slots=True)
class GeneratedApiKey:
    """The three pieces a new key produces.

    `plaintext` is returned to the caller once and then discarded; `key_id` is
    stored and displayed; `key_hash` is stored and compared. Nothing anywhere
    can reconstruct `plaintext` from the other two.
    """

    plaintext: str
    key_id: str
    key_hash: str
    prefix: str


def generate_api_key(*, live: bool = True) -> GeneratedApiKey:
    """Mint `ierp_live_<key_id>_<secret>`.

    The key id is embedded so verification is one indexed lookup followed by
    one constant-time digest comparison, rather than a hash comparison against
    every key in the table.
    """
    prefix = API_KEY_PREFIX_LIVE if live else API_KEY_PREFIX_TEST
    key_id = secrets.token_hex(8)                 # 16 chars, public
    secret = secrets.token_urlsafe(TOKEN_BYTES)   # the actual entropy
    plaintext = f"{prefix}_{key_id}_{secret}"
    return GeneratedApiKey(
        plaintext=plaintext,
        key_id=key_id,
        key_hash=hash_token(plaintext),
        prefix=prefix,
    )


def parse_api_key(plaintext: str) -> str | None:
    """Extract the key id from a presented key, or None if malformed.

    Deliberately strict: an unparseable key is rejected before it touches the
    database, so a malformed header cannot become a table scan.
    """
    parts = plaintext.split("_")
    if len(parts) < 4:
        return None
    if f"{parts[0]}_{parts[1]}" not in (API_KEY_PREFIX_LIVE, API_KEY_PREFIX_TEST):
        return None
    key_id = parts[2]
    if len(key_id) != 16 or not all(c in "0123456789abcdef" for c in key_id):
        return None
    return key_id


# ---------------------------------------------------------------------------
# JWT — signed, not encrypted
# ---------------------------------------------------------------------------
class TokenError(Exception):
    """A JWT failed to verify. Never carries the token itself."""


def _signing_key() -> str:
    """The HMAC key.

    In production a missing `SECRET_KEY` is fatal: a predictable signing key
    means anyone can mint an admin session. In development a per-process
    random key is generated instead, which is safe (it dies with the process)
    and keeps `git clone && uvicorn` working with no setup.
    """
    key = settings.SECRET_KEY
    if key:
        return key
    if settings.is_production:
        raise RuntimeError(
            "SECRET_KEY must be set in production — refusing to sign tokens "
            "with a generated key."
        )
    return _EPHEMERAL_KEY


_EPHEMERAL_KEY = secrets.token_urlsafe(48)

JWT_ALGORITHM = "HS256"


def encode_jwt(claims: dict[str, Any], *, ttl_seconds: int) -> str:
    """Sign a claim set.

    `iat`, `exp` and `jti` are always added here rather than by the caller:
    a token minted without an expiry is a permanent credential, and that must
    not be possible by omission.
    """
    import jwt as pyjwt

    now = datetime.now(timezone.utc)
    payload = {
        **claims,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": secrets.token_urlsafe(12),
        "iss": settings.JWT_ISSUER,
    }
    return pyjwt.encode(payload, _signing_key(), algorithm=JWT_ALGORITHM)


def decode_jwt(token: str, *, expected_type: str | None = None) -> dict[str, Any]:
    """Verify and return the claims, or raise `TokenError`.

    The algorithm is pinned to a single value. Accepting a list — or worse,
    trusting the header's `alg` — is the classic JWT confusion attack, where
    `alg: none` or an HMAC-signed token verified as RSA lets an attacker mint
    whatever they like.
    """
    import jwt as pyjwt

    try:
        claims = pyjwt.decode(
            token, _signing_key(),
            algorithms=[JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            options={"require": ["exp"], "verify_iss": True},
        )
    except pyjwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc

    if expected_type is not None and claims.get("typ") != expected_type:
        # An access token presented where a refresh token belongs, or the
        # reverse. Without this check the two are interchangeable and a
        # long-lived refresh token becomes a long-lived session.
        raise TokenError(
            f"expected a '{expected_type}' token, got '{claims.get('typ')}'"
        )
    return claims


# ---------------------------------------------------------------------------
# Envelope encryption for stored secrets
# ---------------------------------------------------------------------------
CURRENT_KEY_VERSION = 1


def _encryption_key(version: int = CURRENT_KEY_VERSION) -> bytes:
    """Derive a 32-byte AES key from the configured master secret.

    HKDF-style derivation with the version in the info string, so rotating to
    version 2 produces an entirely different key while version-1 ciphertexts
    remain decryptable.
    """
    master = settings.ENCRYPTION_KEY or settings.SECRET_KEY or _EPHEMERAL_KEY
    if settings.is_production and not (settings.ENCRYPTION_KEY or settings.SECRET_KEY):
        raise RuntimeError(
            "ENCRYPTION_KEY or SECRET_KEY must be set in production — "
            "refusing to encrypt with a generated key that dies on restart."
        )
    return hashlib.pbkdf2_hmac(
        "sha256", master.encode(), f"ierp-envelope-v{version}".encode(),
        200_000, dklen=32,
    )


def encrypt_secret(plaintext: str, *, version: int = CURRENT_KEY_VERSION) -> bytes:
    """AES-256-GCM. Output is `version(1) || nonce(12) || ciphertext+tag`.

    The version travels with the ciphertext so decryption never has to guess
    which key was used.
    """
    nonce = os.urandom(12)
    aes = AESGCM(_encryption_key(version))
    blob = aes.encrypt(nonce, plaintext.encode(), None)
    return bytes([version]) + nonce + blob


def decrypt_secret(blob: bytes) -> str:
    """Reverse `encrypt_secret`. Raises on tampering — GCM authenticates."""
    if len(blob) < 14:
        raise ValueError("ciphertext too short to be valid")
    version, nonce, payload = blob[0], blob[1:13], blob[13:]
    aes = AESGCM(_encryption_key(version))
    return aes.decrypt(nonce, payload, None).decode()


# ---------------------------------------------------------------------------
# Webhook signatures
# ---------------------------------------------------------------------------
def sign_payload(payload: bytes, secret: str) -> str:
    """HMAC-SHA256 hex digest, the convention every payment provider uses."""
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    return secrets.compare_digest(sign_payload(payload, secret), signature)


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
def csrf_token(session_id: str) -> str:
    """A CSRF token bound to the session.

    Signed rather than random-and-stored: the server can verify it with no
    lookup, and it is useless in another session. This is the "signed
    double-submit cookie" pattern — the naive version, where the cookie value
    is simply echoed in a header, is defeated by a subdomain that can set
    cookies.
    """
    return hmac.new(
        _signing_key().encode(), f"csrf:{session_id}".encode(), hashlib.sha256,
    ).hexdigest()


def verify_csrf(session_id: str, token: str) -> bool:
    return secrets.compare_digest(csrf_token(session_id), token or "")


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
def new_id() -> str:
    """A UUID4-shaped identifier for users and sessions."""
    import uuid

    return str(uuid.uuid4())


def fingerprint(*parts: str) -> str:
    """A short stable hash, used for error grouping and cache keys."""
    return hashlib.sha1("|".join(parts).encode(), usedforsecurity=False).hexdigest()[:40]


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def canonical_json(value: Any) -> str:
    """Stable JSON for hashing — sorted keys, no incidental whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

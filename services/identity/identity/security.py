"""Password hashing and refresh-token generation (docs §5.2 credentials rules)."""

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher(memory_cost=65536, time_cost=3, parallelism=4)

# Verified when login hits an unknown email, so both paths cost one argon2
# call — otherwise response timing would leak which emails exist.
_DUMMY_HASH = _hasher.hash("definitely-not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        _hasher.verify(password_hash or _DUMMY_HASH, password)
        return True
    except VerifyMismatchError:
        return False


def new_refresh_token() -> tuple[str, str]:
    """Return (opaque token for the client, sha256 hex we store).

    We store only the hash: a leaked identity_db must not yield usable
    refresh tokens.
    """
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

"""RSA key generation, JWKS rendering, and token issuing.

Only the Identity service (and tests) should import this module — everything
else either verifies tokens (verifier.py, at the edge) or trusts identity
headers (context.py, inside the VPC). Keeping signing concerns in one place
is what makes ADR-0005's "one hardened verifier" claim auditable.
"""

import base64
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .context import Role


def _b64url_uint(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass(frozen=True)
class RsaKey:
    kid: str
    private_pem: str
    public_jwk: dict[str, str] = field(repr=False)


def generate_rsa_key() -> RsaKey:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    numbers = key.public_key().public_numbers()
    n_bytes = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    kid = hashlib.sha256(n_bytes).hexdigest()[:16]

    public_jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }
    return RsaKey(kid=kid, private_pem=private_pem, public_jwk=public_jwk)


def jwks(keys: list[RsaKey]) -> dict[str, Any]:
    """Render the public document served at /.well-known/jwks.json.

    Takes a list so rotation works out of the box: publish current + next,
    switch signing, retire the old one after max token TTL (docs §5.2).
    """
    return {"keys": [k.public_jwk for k in keys]}


class TokenIssuer:
    """Issues RS256 access tokens. Lives in the lib so token *shape* has one owner."""

    def __init__(self, key: RsaKey, *, issuer: str, audience: str, ttl_seconds: int = 900):
        self._key = key
        self._issuer = issuer
        self._audience = audience
        self._ttl = ttl_seconds

    def issue(
        self,
        *,
        sub: str,
        role: "Role | str",
        restaurant_id: str | None = None,
        rider_id: str | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": sub,
            "role": str(Role(role)),  # typos die HERE, never in a minted token
            "iat": now,
            "exp": now + self._ttl,
            "jti": uuid.uuid4().hex,
        }
        if restaurant_id is not None:
            claims["restaurant_id"] = restaurant_id
        if rider_id is not None:
            claims["rider_id"] = rider_id
        return jwt.encode(
            claims, self._key.private_pem, algorithm="RS256", headers={"kid": self._key.kid}
        )

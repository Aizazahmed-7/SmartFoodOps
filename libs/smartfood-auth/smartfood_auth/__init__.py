"""smartfood-auth — the auth machinery from ADR-0005.

Identity issues (keys.py) → edge verifies (verifier.py) → services trust
headers (context.py) → edge/activities stamp them (stamp.py).
"""

from .context import (
    ROLES,
    STRIP_HEADERS,
    Auth,
    AuthContext,
    get_auth_context,
    require_role,
)
from .keys import RsaKey, TokenIssuer, generate_rsa_key, jwks
from .stamp import context_from_claims, headers_for
from .verifier import JwksVerifier

__all__ = [
    "ROLES",
    "STRIP_HEADERS",
    "Auth",
    "AuthContext",
    "JwksVerifier",
    "RsaKey",
    "TokenIssuer",
    "context_from_claims",
    "generate_rsa_key",
    "get_auth_context",
    "headers_for",
    "jwks",
    "require_role",
]

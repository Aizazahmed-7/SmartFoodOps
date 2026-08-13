"""smartfood-auth — the auth machinery from ADR-0005.

Identity issues (keys.py) → edge verifies (verifier.py) → services trust
headers (context.py) → edge/activities stamp them (stamp.py).
"""

from .context import (
    ROLES,
    STRIP_HEADERS,
    Auth,
    AuthContext,
    Forbidden,
    MissingIdentity,
    Role,
    get_auth_context,
    require_role,
    require_system,
)
from .keys import RsaKey, TokenIssuer, generate_rsa_key, jwks
from .stamp import context_from_claims, headers_for, internal_headers
from .verifier import JwksVerifier

__all__ = [
    "ROLES",
    "STRIP_HEADERS",
    "Auth",
    "AuthContext",
    "Forbidden",
    "MissingIdentity",
    "Role",
    "JwksVerifier",
    "RsaKey",
    "TokenIssuer",
    "context_from_claims",
    "generate_rsa_key",
    "get_auth_context",
    "headers_for",
    "internal_headers",
    "jwks",
    "require_role",
    "require_system",
]

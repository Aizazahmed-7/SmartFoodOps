"""JWKS-based token verification — the edge's half of the auth design.

Used by edge-bff (and, in phase 2, the WS/SSE gateways). Domain services never
import this: they consume identity headers via context.py (ADR-0005).
"""

import json
import time
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm


class JwksVerifier:
    """Verifies RS256 tokens against a cached JWKS.

    Cache behavior (docs §5.2): keys are cached in-process for `cache_ttl`
    seconds with a refetch on unknown `kid` — that is what makes key rotation
    safe: a token signed by the *next* key arrives, its kid is unknown, we
    refetch once and find it.
    """

    def __init__(
        self,
        jwks_url: str,
        *,
        issuer: str,
        audience: str,
        cache_ttl: float = 600.0,
        min_refetch_interval: float = 5.0,
        http: httpx.AsyncClient | None = None,
    ):
        self._jwks_url = jwks_url
        self._issuer = issuer
        self._audience = audience
        self._cache_ttl = cache_ttl
        self._min_refetch_interval = min_refetch_interval
        self._http = http or httpx.AsyncClient(timeout=5.0)
        self._keys: dict[str, Any] = {}
        self._fetched_at: float = 0.0

    async def _refresh(self) -> None:
        resp = await self._http.get(self._jwks_url)
        resp.raise_for_status()
        doc = resp.json()
        self._keys = {
            k["kid"]: RSAAlgorithm.from_jwk(json.dumps(k))
            for k in doc.get("keys", [])
            if k.get("kty") == "RSA"
        }
        self._fetched_at = time.monotonic()

    def _stale(self) -> bool:
        return (time.monotonic() - self._fetched_at) > self._cache_ttl

    async def verify(self, token: str) -> dict[str, Any]:
        """Return the verified claims, or raise jwt.InvalidTokenError (or a subclass)."""
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256":
            raise jwt.InvalidTokenError("unexpected alg")

        kid = header.get("kid")
        if kid is None:
            raise jwt.InvalidTokenError("missing kid")

        if kid not in self._keys or self._stale():
            # Clamp: an attacker spraying unknown kids must not turn every bad
            # token into a JWKS fetch against Identity (DoS lever).
            if (time.monotonic() - self._fetched_at) >= self._min_refetch_interval:
                await self._refresh()
        key = self._keys.get(kid)
        if key is None:
            raise jwt.InvalidTokenError("unknown kid")

        return jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            issuer=self._issuer,
            audience=self._audience,
        )

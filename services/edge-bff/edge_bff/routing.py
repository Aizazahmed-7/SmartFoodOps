"""The route table — which path prefix goes to which service, and who may call it.

Modes:
  public       — no token ever (how you GET a token in the first place)
  public_read  — GET/HEAD are anonymous (browse!), writes need a token
  auth         — every method needs a verified token

Longest prefix wins, so /v1/auth/login (public) beats /v1/auth (auth).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

RouteMode = Literal["public", "public_read", "auth"]


@dataclass(frozen=True)
class Rule:
    prefix: str
    upstream: str  # settings attribute name — bind with resolve() at startup
    mode: RouteMode
    # Rate-limit class. "auth" is explicit on the credential endpoints (the
    # attack there is guessing, so the budget is tight regardless of verb);
    # None = derive from the verb at request time (GET/HEAD -> "read",
    # writes -> "write"). See limit_class_for().
    limit: str | None = None


@dataclass(frozen=True)
class ResolvedRule:
    """A Rule with its upstream attribute bound to a concrete base URL —
    built ONCE in create_app so the proxy never getattr()s per request."""

    prefix: str
    base_url: str
    mode: RouteMode
    limit: str | None = None


class _HasPrefix(Protocol):
    @property
    def prefix(self) -> str: ...  # read-only: frozen dataclasses qualify


def resolve(rules: "Sequence[Rule]", settings: object) -> list[ResolvedRule]:
    return [
        ResolvedRule(rule.prefix, getattr(settings, rule.upstream), rule.mode, rule.limit)
        for rule in rules
    ]


RULES = [
    Rule("/v1/auth/register", "identity_base_url", "public", limit="auth"),
    Rule("/v1/auth/login", "identity_base_url", "public", limit="auth"),
    Rule("/v1/auth/refresh", "identity_base_url", "public", limit="auth"),
    Rule("/v1/auth", "identity_base_url", "auth"),
    Rule("/v1/me", "identity_base_url", "auth"),
    Rule("/v1/restaurants", "catalog_base_url", "public_read"),
    Rule("/v1/menus", "catalog_base_url", "public_read"),
    Rule("/v1/search", "catalog_base_url", "public_read"),
    Rule("/v1/inventory", "inventory_base_url", "auth"),
    Rule("/v1/orders", "order_base_url", "auth"),
    Rule("/v1/quote", "order_base_url", "auth"),
    # No collision with catalog's /v1/restaurants: the matcher requires a
    # "/" right after the prefix, and "restaurants"[10] is "s", not "/".
    Rule("/v1/restaurant", "order_base_url", "auth"),
    Rule("/v1/notifications", "notification_base_url", "auth"),
    # Only the ticket POST — the SSE stream rides /sse/track/* straight
    # from the gateway to order (the ticket IS its auth, FR-38).
    Rule("/v1/track", "order_base_url", "auth"),
    # Longer prefix beats "/v1/restaurant" -> order: the owner's metrics
    # view rides the analytics service, everything else under /v1/restaurant
    # stays kitchen ops.
    Rule("/v1/restaurant/analytics", "analytics_base_url", "auth"),
]


def match[R: _HasPrefix](path: str, rules: Sequence[R]) -> R | None:
    best: R | None = None
    for rule in rules:
        if path == rule.prefix or path.startswith(rule.prefix + "/"):
            if best is None or len(rule.prefix) > len(best.prefix):
                best = rule
    return best


def needs_auth(rule: "Rule | ResolvedRule", method: str) -> bool:
    if rule.mode == "public":
        return False
    if rule.mode == "public_read":
        return method.upper() not in ("GET", "HEAD")
    return True


def limit_class_for(rule: "Rule | ResolvedRule", method: str) -> str:
    """The bucket a request draws from. Explicit tag first (auth), then the
    verb: reads are cheap and plentiful, writes cost money paths."""
    if rule.limit is not None:
        return rule.limit
    return "read" if method.upper() in ("GET", "HEAD") else "write"

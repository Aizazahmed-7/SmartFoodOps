"""The route table — which path prefix goes to which service, and who may call it.

Modes:
  public       — no token ever (how you GET a token in the first place)
  public_read  — GET/HEAD are anonymous (browse!), writes need a token
  auth         — every method needs a verified token

Longest prefix wins, so /v1/auth/login (public) beats /v1/auth (auth).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    prefix: str
    upstream: str  # settings attribute name, resolved at startup
    mode: str  # public | public_read | auth


RULES = [
    Rule("/v1/auth/register", "identity_base_url", "public"),
    Rule("/v1/auth/login", "identity_base_url", "public"),
    Rule("/v1/auth/refresh", "identity_base_url", "public"),
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
]


def match(path: str) -> Rule | None:
    best: Rule | None = None
    for rule in RULES:
        if path == rule.prefix or path.startswith(rule.prefix + "/"):
            if best is None or len(rule.prefix) > len(best.prefix):
                best = rule
    return best


def needs_auth(rule: Rule, method: str) -> bool:
    if rule.mode == "public":
        return False
    if rule.mode == "public_read":
        return method.upper() not in ("GET", "HEAD")
    return True

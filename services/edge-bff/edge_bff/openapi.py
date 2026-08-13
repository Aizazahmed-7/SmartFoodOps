"""The gateway's merged OpenAPI document — the FE's single contract.

Each service publishes its own FastAPI-generated spec; the edge fetches
them and merges ONLY the paths its allowlist routes (routing.RULES). The
route table is the source of truth for "callable from outside", so the
docs can never advertise an unroutable path (goodbye /v1/internal/*) and
never miss a newly-routed one. Auth-required operations are marked with a
bearer security scheme derived from the same table.
"""

import json
from typing import Any

from .routing import RULES, match, needs_auth

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head"}

INFO = {
    "title": "SmartFoodOps API",
    "version": "1.0.0",
    "description": (
        "Everything callable through the gateway, merged from the owning "
        "services and filtered by the edge allowlist. Operations marked "
        "with bearerAuth require `Authorization: Bearer <access token>`."
    ),
}


def _service_label(upstream: str) -> str:
    return upstream.removesuffix("_base_url").replace("-", " ").title().replace(" ", "")


def merge_specs(specs: dict[str, dict[str, Any]], missing: list[str]) -> dict[str, Any]:
    """specs: upstream-attr → that service's openapi doc, in iteration order.
    First service to claim a schema name keeps it; identical duplicates
    dedupe silently; genuine collisions get a ServiceName_ prefix (with
    $refs rewritten)."""
    merged_paths: dict[str, Any] = {}
    merged_schemas: dict[str, Any] = {}

    for upstream in sorted(specs):
        spec = specs[upstream]
        schemas = spec.get("components", {}).get("schemas", {})
        renamed: dict[str, str] = {}
        for name, schema in schemas.items():
            if name in merged_schemas and merged_schemas[name] != schema:
                renamed[name] = f"{_service_label(upstream)}_{name}"
        if renamed:
            text = json.dumps(spec)
            for old, new in renamed.items():
                text = text.replace(
                    f'"#/components/schemas/{old}"', f'"#/components/schemas/{new}"'
                )
            spec = json.loads(text)
            schemas = spec.get("components", {}).get("schemas", {})

        for name, schema in schemas.items():
            merged_schemas[renamed.get(name) or str(name)] = schema

        for path, operations in spec.get("paths", {}).items():
            rule = match(path, RULES)  # templates keep their prefix: /v1/x/{id} matches /v1/x
            if rule is None:
                continue  # not in the allowlist → not callable → not documented
            for method, operation in operations.items():
                if method not in _HTTP_METHODS:
                    continue
                if needs_auth(rule, method.upper()):
                    operation["security"] = [{"bearerAuth": []}]
                merged_paths.setdefault(path, {})[method] = operation

    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": INFO,
        "servers": [{"url": "/"}],  # relative: whatever host serves the doc
        "paths": dict(sorted(merged_paths.items())),
        "components": {
            "schemas": dict(sorted(merged_schemas.items())),
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
            },
        },
    }
    if missing:
        # Partial docs beat no docs — but say so, loudly and machine-readably.
        doc["x-unavailable-upstreams"] = sorted(missing)
    return doc

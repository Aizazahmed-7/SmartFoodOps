"""Confluent Schema Registry client (async, httpx).

Registration is idempotent on the registry side (same schema → same id), and
cached here per subject. BACKWARD_TRANSITIVE compatibility is asserted on
every registration — the CI gate from docs §8 enforced at runtime too: a
producer whose schema can't evolve compatibly fails to START, not mid-drain.
"""

import json
from typing import Any

import httpx


class SchemaRegistryError(RuntimeError):
    pass


class SchemaRegistry:
    def __init__(self, base_url: str, http: httpx.AsyncClient | None = None):
        self._base = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._id_by_subject: dict[str, int] = {}
        self._schema_by_id: dict[int, dict[str, Any]] = {}

    async def register(self, subject: str, schema: dict[str, Any]) -> int:
        """Returns the registry's schema id, caching per subject."""
        if subject in self._id_by_subject:
            return self._id_by_subject[subject]
        compat = await self._http.put(
            f"{self._base}/config/{subject}",
            json={"compatibility": "BACKWARD_TRANSITIVE"},
        )
        if compat.status_code >= 300:
            raise SchemaRegistryError(
                f"could not set compatibility for {subject}: {compat.status_code}"
            )
        resp = await self._http.post(
            f"{self._base}/subjects/{subject}/versions",
            json={"schema": json.dumps(schema), "schemaType": "AVRO"},
        )
        if resp.status_code >= 300:
            raise SchemaRegistryError(
                f"registration refused for {subject}: {resp.status_code} {resp.text[:200]}"
            )
        schema_id = int(resp.json()["id"])
        self._id_by_subject[subject] = schema_id
        self._schema_by_id[schema_id] = schema
        return schema_id

    async def schema_by_id(self, schema_id: int) -> dict[str, Any]:
        """Consumer-side lookup: resolve the id embedded in the wire format."""
        if schema_id in self._schema_by_id:
            return self._schema_by_id[schema_id]
        resp = await self._http.get(f"{self._base}/schemas/ids/{schema_id}")
        if resp.status_code >= 300:
            raise SchemaRegistryError(f"unknown schema id {schema_id}: {resp.status_code}")
        schema = json.loads(resp.json()["schema"])
        self._schema_by_id[schema_id] = schema
        return schema

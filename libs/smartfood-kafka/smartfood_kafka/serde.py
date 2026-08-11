"""Confluent wire format ⇄ Avro records.

Wire format: magic byte 0x00 + 4-byte big-endian schema id + Avro binary.
Byte-identical to what Confluent serializers (and therefore the W3 Debezium
path) produce — the parity requirement of ADR-0012 starts at the bytes.
"""

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
# (fastavro's public API is partially annotated; the casts below are the
# typed boundary.)

import io
import struct
from typing import Any, cast

from fastavro import parse_schema, schemaless_reader, schemaless_writer

from .registry import SchemaRegistry

_MAGIC = 0


class SerdeError(RuntimeError):
    pass


class AvroSerde:
    def __init__(self, registry: SchemaRegistry):
        self._registry = registry
        self._parsed: dict[int, Any] = {}  # fastavro parse cache, keyed by schema id

    async def encode(self, subject: str, schema: dict[str, Any], record: dict[str, Any]) -> bytes:
        schema_id = await self._registry.register(subject, schema)
        if schema_id not in self._parsed:
            self._parsed[schema_id] = parse_schema(schema)
        buffer = io.BytesIO()
        schemaless_writer(buffer, self._parsed[schema_id], record)
        return struct.pack(">bI", _MAGIC, schema_id) + buffer.getvalue()

    async def decode(self, data: bytes) -> dict[str, Any]:
        if len(data) < 5 or data[0] != _MAGIC:
            raise SerdeError("not Confluent wire format (bad magic byte)")
        (schema_id,) = struct.unpack(">I", data[1:5])
        if schema_id not in self._parsed:
            schema = await self._registry.schema_by_id(schema_id)
            self._parsed[schema_id] = parse_schema(schema)
        return cast(
            dict[str, Any],
            schemaless_reader(io.BytesIO(data[5:]), self._parsed[schema_id]),
        )

"""Base classes for API DTOs — docs/api-standards.md §3.

StrictModel: unknown fields are rejected (extra=forbid), so a typo'd field
name is a 422 instead of a silently ignored no-op. Every request DTO in
every service inherits from this; response models may stay permissive.
"""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

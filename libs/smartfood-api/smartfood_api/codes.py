"""The error-code catalog (docs/api-standards.md §2), as symbols.

These codes cross the widest boundary in the system — minted in service
routes, matched in the frontend's client. The prose table remains the
governance registry ("adding a code = a PR to the table"); this enum is its
compile-time mirror, and ApiError only accepts members — a typo'd code is
now a type error at the call site instead of a broken client flow.

StrEnum members serialize as their plain string values: the envelope on
the wire is byte-identical to before.
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    IDEMPOTENCY_KEY_REUSE = "IDEMPOTENCY_KEY_REUSE"
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
    AUTH_REFRESH_REUSED = "AUTH_REFRESH_REUSED"
    FORBIDDEN_ROLE = "FORBIDDEN_ROLE"
    NOT_FOUND = "NOT_FOUND"
    GRANT_CONFLICT = "GRANT_CONFLICT"
    CATEGORY_NOT_EMPTY = "CATEGORY_NOT_EMPTY"
    ORDER_NOT_CANCELLABLE = "ORDER_NOT_CANCELLABLE"
    ORDER_ALREADY_DECIDED = "ORDER_ALREADY_DECIDED"
    ORDER_STATE_CONFLICT = "ORDER_STATE_CONFLICT"
    ITEM_UNAVAILABLE = "ITEM_UNAVAILABLE"
    RESTAURANT_AT_CAPACITY = "RESTAURANT_AT_CAPACITY"
    RESTAURANT_CLOSED = "RESTAURANT_CLOSED"
    PRICE_CHANGED = "PRICE_CHANGED"
    PAYMENT_DECLINED = "PAYMENT_DECLINED"
    RATE_LIMITED = "RATE_LIMITED"
    ADMISSION_SHED = "ADMISSION_SHED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"

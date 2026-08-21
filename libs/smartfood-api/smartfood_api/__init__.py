"""smartfood-api — the HTTP contract machinery from docs/api-standards.md."""

from .codes import ErrorCode
from .errors import ApiError, envelope, install_error_handlers
from .models import StrictModel
from .observability import mount_observability

__all__ = [
    "ApiError",
    "ErrorCode",
    "StrictModel",
    "envelope",
    "install_error_handlers",
    "mount_observability",
]

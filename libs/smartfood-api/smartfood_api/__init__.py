"""smartfood-api — the HTTP contract machinery from docs/api-standards.md."""

from .errors import ApiError, envelope, install_error_handlers
from .models import StrictModel

__all__ = ["ApiError", "StrictModel", "envelope", "install_error_handlers"]

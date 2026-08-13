"""edge-bff settings — upstream URLs and token verification parameters.

issuer/audience must match what Identity stamps into tokens; both sides
default to the same values and compose injects the same envs.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    identity_base_url: str = "http://localhost:8001"
    catalog_base_url: str = "http://localhost:8002"
    inventory_base_url: str = "http://localhost:8005"
    order_base_url: str = "http://localhost:8006"
    notification_base_url: str = "http://localhost:8008"

    identity_jwks_url: str = "http://localhost:8001/.well-known/jwks.json"
    token_issuer: str = "http://identity:8001"
    token_audience: str = "sfo-api"
    jwks_cache_ttl: float = 600.0

    proxy_timeout_seconds: float = 10.0

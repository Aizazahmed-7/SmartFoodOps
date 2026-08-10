"""The seeder against the REAL three-app chain in-process: seed → edge-bff
(JWT verify + header stamping) → identity/catalog, including the real
Catalog→Identity grant hop. The closest thing to `make seed` that runs
without infrastructure."""

from typing import Any

import httpx
import pytest
from catalog.adapters.identity_grants import IdentityGrantsClient
from catalog.config import Settings as CatalogSettings
from catalog.main import create_app as create_catalog
from edge_bff.config import Settings as EdgeSettings
from edge_bff.main import create_app as create_edge
from fastapi.testclient import TestClient
from identity.config import Settings as IdentitySettings
from identity.main import create_app as create_identity
from seed.main import CITIES, TEMPLATES, SeedError, seed
from smartfood_auth import JwksVerifier


class _NullCache:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def acquire_lock(self, key: str, ttl_ms: int) -> bool:
        return True

    async def release_lock(self, key: str) -> None: ...


class _NullSearch:
    async def search(self, **kwargs) -> list[dict]:
        return []


class HostRouter(httpx.AsyncBaseTransport):
    """Routes by hostname to in-process ASGI apps — the compose network,
    minus the network."""

    def __init__(self, apps: dict[str, Any]):
        self._transports = {
            host: httpx.ASGITransport(app=app) for host, app in apps.items()
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transports[request.url.host].handle_async_request(request)


@pytest.fixture()
def edge(tmp_path):
    identity_app = create_identity(
        IdentitySettings(
            database_url="sqlite+aiosqlite://",
            create_all=True,
            signing_key_path=str(tmp_path / "key.pem"),
            token_issuer="http://identity.test",
        )
    )
    # Catalog's grant calls travel a REAL httpx hop into the identity app.
    grants = IdentityGrantsClient(
        "http://identity.test",
        httpx.AsyncClient(transport=httpx.ASGITransport(app=identity_app)),
        retry_delay=0.0,
    )
    catalog_app = create_catalog(
        CatalogSettings(database_url="sqlite+aiosqlite://", create_all=True),
        grants=grants,
        cache=_NullCache(),
        search=_NullSearch(),
    )
    router = HostRouter({"identity.test": identity_app, "catalog.test": catalog_app})
    edge_app = create_edge(
        EdgeSettings(
            identity_base_url="http://identity.test",
            catalog_base_url="http://catalog.test",
            identity_jwks_url="http://identity.test/.well-known/jwks.json",
            token_issuer="http://identity.test",
        ),
        http=httpx.AsyncClient(transport=router),
        verifier=JwksVerifier(
            "http://identity.test/.well-known/jwks.json",
            issuer="http://identity.test",
            audience="sfo-api",
            http=httpx.AsyncClient(transport=router),
        ),
    )
    # TestClient contexts run all three lifespans (create_all etc.).
    with TestClient(identity_app), TestClient(catalog_app), TestClient(edge_app):
        yield edge_app


async def test_seed_creates_everything_then_replays_clean(edge):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge), base_url="http://gw.test"
    ) as client:
        first = await seed(client)
        expected = len(CITIES) * len(TEMPLATES)
        assert first == {"created": expected, "replayed": 0}

        # Spot-check the world through the public APIs (via the edge):
        browse = (await client.get("/v1/restaurants", params={"city": "springfield"})).json()
        assert len(browse["restaurants"]) == len(TEMPLATES)
        biryani = next(r for r in browse["restaurants"] if r["name"] == "Biryani House")
        menu = (await client.get(f"/v1/menus/{biryani['id']}")).json()
        assert [c["name"] for c in menu["categories"]] == ["Mains", "Sides"]
        mains = menu["categories"][0]["items"]
        assert mains[0]["name"] == "Chicken Biryani"
        assert mains[0]["tags"] == ["halal", "spicy"]
        assert mains[0]["modifier_groups"][0]["options"][1]["price_delta_cents"] == 300

        # Idempotency: the second run creates NOTHING and changes nothing.
        second = await seed(client)
        assert second == {"created": 0, "replayed": expected}
        menu_after = (await client.get(f"/v1/menus/{biryani['id']}")).json()
        assert menu_after == menu  # same version, same content — untouched


async def test_seed_fails_loudly_on_broken_gateway():
    async def broken(scope, receive, send):
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.body", "body": b"boom"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=broken), base_url="http://gw.test"
    ) as client:
        with pytest.raises(SeedError):
            await seed(client)

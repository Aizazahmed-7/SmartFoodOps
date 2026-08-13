"""The seeder against the REAL four-app chain in-process: seed → edge-bff
(JWT verify + header stamping) → identity/catalog/inventory, including the
real Catalog→Identity grant hop. The closest thing to `make seed` that runs
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
from inventory.config import Settings as InventorySettings
from inventory.main import create_app as create_inventory
from seed.main import CITIES, PASSWORD, SEED_STOCK, TEMPLATES, SeedError, seed
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
        self._transports = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

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
    inventory_app = create_inventory(
        InventorySettings(database_url="sqlite+aiosqlite://", create_all=True)
    )
    router = HostRouter(
        {
            "identity.test": identity_app,
            "catalog.test": catalog_app,
            "inventory.test": inventory_app,
        }
    )
    edge_app = create_edge(
        EdgeSettings(
            identity_base_url="http://identity.test",
            catalog_base_url="http://catalog.test",
            inventory_base_url="http://inventory.test",
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
    # TestClient contexts run all four lifespans (create_all etc.).
    with (
        TestClient(identity_app),
        TestClient(catalog_app),
        TestClient(inventory_app),
        TestClient(edge_app),
    ):
        yield edge_app


def test_template_names_are_globally_unique():
    """Duplicate names across cities read as broken data in browse — the
    dataset itself must make same-name rows impossible."""
    names = [t["name"] for t in TEMPLATES]
    assert len(names) == len(set(names))
    assert {t["city"] for t in TEMPLATES} == set(CITIES)


async def test_seed_creates_everything_then_replays_clean(edge):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge), base_url="http://gw.test"
    ) as client:
        first = await seed(client)
        expected = len(TEMPLATES)
        assert first == {"created": expected, "replayed": 0}

        # Spot-check the world through the public APIs (via the edge):
        # each city holds exactly its templates, no duplicates.
        for city in CITIES:
            browse = (await client.get("/v1/restaurants", params={"city": city})).json()
            names = [r["name"] for r in browse["restaurants"]]
            assert sorted(names) == sorted(t["name"] for t in TEMPLATES if t["city"] == city)

        browse = (await client.get("/v1/restaurants", params={"city": "springfield"})).json()
        biryani = next(r for r in browse["restaurants"] if r["name"] == "Biryani House")
        menu = (await client.get(f"/v1/menus/{biryani['id']}")).json()
        assert [c["name"] for c in menu["categories"]] == ["Mains", "Sides"]
        mains = menu["categories"][0]["items"]
        assert mains[0]["name"] == "Chicken Biryani"
        assert mains[0]["tags"] == ["halal", "spicy"]
        assert mains[0]["description"]  # descriptions feed FTS search
        assert mains[0]["modifier_groups"][0]["options"][1]["price_delta_cents"] == 300

        # The multi-select shape (checkbox UI path) made it through intact.
        barn = next(r for r in browse["restaurants"] if r["name"] == "Burger Barn")
        barn_menu = (await client.get(f"/v1/menus/{barn['id']}")).json()
        smash = barn_menu["categories"][0]["items"][0]
        groups = {g["name"]: g for g in smash["modifier_groups"]}
        assert set(groups) == {"Size", "Add-ons"}
        assert groups["Add-ons"]["min_select"] == 0
        assert groups["Add-ons"]["max_select"] == 3

        # STRICT stock: every seeded item is stocked through the real
        # inventory API (owner's own bearer token, via the edge).
        login = await client.post(
            "/v1/auth/login",
            json={
                "email": "owner-springfield-biryani-house@demo.smartfood.dev",
                "password": PASSWORD,
            },
        )
        bearer = {"Authorization": f"Bearer {login.json()['access_token']}"}
        stock_url = f"/v1/inventory/restaurants/{biryani['id']}/stock"
        rows = (await client.get(stock_url, headers=bearer)).json()["items"]
        menu_item_count = sum(len(c["items"]) for c in menu["categories"])
        assert len(rows) == menu_item_count
        assert all(r["available"] == SEED_STOCK for r in rows)

        # An admin's manual count must survive re-seeding.
        touched = rows[0]["item_id"]
        await client.put(f"{stock_url}/{touched}", json={"available": 5}, headers=bearer)

        # The demo customer exists with exactly one saved address (S9) —
        # and the second run below must not duplicate it.
        customer_pair = (
            await client.post(
                "/v1/auth/login",
                json={"email": "customer@demo.smartfood.dev", "password": PASSWORD},
            )
        ).json()
        customer_bearer = {"Authorization": f"Bearer {customer_pair['access_token']}"}
        addresses = (await client.get("/v1/me/addresses", headers=customer_bearer)).json()
        assert [a["label"] for a in addresses] == ["home"]

        # Idempotency: the second run creates NOTHING and changes nothing.
        second = await seed(client)
        assert second == {"created": 0, "replayed": expected}
        addresses_after = (await client.get("/v1/me/addresses", headers=customer_bearer)).json()
        assert addresses_after == addresses  # no duplicate address
        menu_after = (await client.get(f"/v1/menus/{biryani['id']}")).json()
        assert menu_after == menu  # same version, same content — untouched
        rows_after = {
            r["item_id"]: r["available"]
            for r in (await client.get(stock_url, headers=bearer)).json()["items"]
        }
        assert rows_after[touched] == 5  # admin's change survived
        assert all(v == SEED_STOCK for i, v in rows_after.items() if i != touched)

        # An item added AFTER seeding (no stock row yet — the consumer isn't
        # in this chain) gets topped up by the next replay.
        cat_id = menu["categories"][0]["id"]
        new_item = (
            await client.post(
                f"/v1/restaurants/{biryani['id']}/items",
                json={"category_id": cat_id, "name": "Late Addition", "price_cents": 500},
                headers=bearer,
            )
        ).json()
        await seed(client)
        rows_final = {
            r["item_id"]: r["available"]
            for r in (await client.get(stock_url, headers=bearer)).json()["items"]
        }
        assert rows_final[new_item["id"]] == SEED_STOCK


async def test_seed_fails_loudly_on_broken_gateway():
    async def broken(scope, receive, send):
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.body", "body": b"boom"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=broken), base_url="http://gw.test"
    ) as client:
        with pytest.raises(SeedError):
            await seed(client)

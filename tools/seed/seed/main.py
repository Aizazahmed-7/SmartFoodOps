"""Deterministic demo data through the REAL APIs (docs/local-dev.md §seeding).

Never raw SQL: every restaurant goes register → onboard (grant fires) →
refresh (claims arrive) → menu CRUD — so seeding exercises auth, validation,
versioning, and the outbox exactly like production traffic.

Idempotent by construction: onboarding replays return the existing
restaurant (200), and a restaurant that already has categories is skipped —
`make seed` twice is safe and changes nothing.
"""

import asyncio
import os
from typing import Any

import httpx

PASSWORD = "demo1234demo"  # every demo login, per docs/local-dev.md

# name, cuisines, {category: [(item, cents, tags, modifier_groups)]}
SIZE = [{"name": "Size", "min_select": 1, "max_select": 1,
         "options": [{"name": "Regular", "rank": 0},
                     {"name": "Large", "price_delta_cents": 300, "rank": 1}]}]
TEMPLATES: list[dict[str, Any]] = [
    {"name": "Biryani House", "cuisines": ["pakistani", "bbq"], "menu": {
        "Mains": [("Chicken Biryani", 1200, ["halal", "spicy"], SIZE),
                  ("Mutton Karahi", 1800, ["halal"], [])],
        "Sides": [("Raita", 200, ["vegetarian"], [])]}},
    {"name": "Burger Barn", "cuisines": ["burgers", "fast-food"], "menu": {
        "Burgers": [("Smash Burger", 950, [], SIZE),
                    ("Veggie Burger", 850, ["vegetarian"], [])],
        "Fries": [("Loaded Fries", 550, [], [])]}},
    {"name": "Pasta Palace", "cuisines": ["italian"], "menu": {
        "Pasta": [("Carbonara", 1400, [], []),
                  ("Arrabbiata", 1200, ["vegetarian", "spicy"], [])]}},
    {"name": "Sushi Spot", "cuisines": ["japanese"], "menu": {
        "Rolls": [("California Roll", 1100, [], []),
                  ("Spicy Tuna", 1300, ["spicy"], [])]}},
    {"name": "Taco Town", "cuisines": ["mexican"], "menu": {
        "Tacos": [("Al Pastor", 900, ["spicy"], []),
                  ("Veggie Taco", 800, ["vegetarian", "vegan"], [])]}},
    {"name": "Curry Corner", "cuisines": ["indian"], "menu": {
        "Curries": [("Butter Chicken", 1500, [], SIZE),
                    ("Chana Masala", 1100, ["vegan"], [])]}},
    {"name": "Pho Real", "cuisines": ["vietnamese"], "menu": {
        "Soups": [("Beef Pho", 1300, [], []),
                  ("Tofu Pho", 1150, ["vegetarian"], [])]}},
    {"name": "Falafel Factory", "cuisines": ["middle-eastern"], "menu": {
        "Wraps": [("Falafel Wrap", 850, ["vegan", "halal"], []),
                  ("Shawarma", 1000, ["halal"], [])]}},
    {"name": "Wok This Way", "cuisines": ["chinese"], "menu": {
        "Stir Fry": [("Kung Pao Chicken", 1250, ["spicy"], []),
                     ("Veg Chow Mein", 1000, ["vegetarian"], [])]}},
    {"name": "Pizza Planet", "cuisines": ["pizza", "italian"], "menu": {
        "Pizzas": [("Margherita", 1300, ["vegetarian"], SIZE),
                   ("Pepperoni", 1500, [], SIZE)]}},
]
CITIES = ["springfield", "shelbyville"]


class SeedError(RuntimeError):
    pass


def _expect(response: httpx.Response, *statuses: int) -> dict[str, Any]:
    if response.status_code not in statuses:
        raise SeedError(
            f"{response.request.method} {response.request.url.path} "
            f"→ {response.status_code}: {response.text[:200]}"
        )
    return response.json()


def _slug(name: str) -> str:
    return name.lower().replace(" ", "-")


async def _seed_restaurant(
    client: httpx.AsyncClient, city: str, template: dict[str, Any]
) -> bool:
    """Returns True if created, False if it already existed (replay)."""
    # .local is a special-use TLD the email validator rejects — .dev is real.
    email = f"owner-{city}-{_slug(template['name'])}@demo.smartfood.dev"
    await client.post(
        "/v1/auth/register", json={"email": email, "password": PASSWORD}
    )  # idempotent no-op if present; login below is the real gate
    pair = _expect(
        await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD}),
        200,
    )
    bearer = {"Authorization": f"Bearer {pair['access_token']}"}

    onboarded = await client.post(
        "/v1/restaurants",
        json={"name": template["name"], "city": city, "cuisines": template["cuisines"]},
        headers=bearer,
    )
    restaurant = _expect(onboarded, 200, 201)
    restaurant_id = restaurant["id"]

    # Refresh: the rotation carries the restaurant_admin grant into the claims.
    fresh = _expect(
        await client.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}),
        200,
    )
    admin = {"Authorization": f"Bearer {fresh['access_token']}"}

    menu = _expect(await client.get(f"/v1/menus/{restaurant_id}"), 200)
    if menu["categories"]:
        return False  # already seeded — replays change nothing

    for rank, (category_name, items) in enumerate(template["menu"].items()):
        category = _expect(
            await client.post(
                f"/v1/restaurants/{restaurant_id}/categories",
                json={"name": category_name, "rank": rank},
                headers=admin,
            ),
            201,
        )
        for item_rank, (item_name, price_cents, tags, groups) in enumerate(items):
            _expect(
                await client.post(
                    f"/v1/restaurants/{restaurant_id}/items",
                    json={
                        "category_id": category["id"],
                        "name": item_name,
                        "price_cents": price_cents,
                        "rank": item_rank,
                        "tags": tags,
                        "modifier_groups": groups,
                    },
                    headers=admin,
                ),
                201,
            )
    return True


async def seed(client: httpx.AsyncClient) -> dict[str, int]:
    created = replayed = 0
    for city in CITIES:
        for template in TEMPLATES:
            if await _seed_restaurant(client, city, template):
                created += 1
            else:
                replayed += 1
    return {"created": created, "replayed": replayed}


async def _amain() -> None:  # pragma: no cover — entrypoint wiring; the seed()
    # flow itself is fully covered by the in-process two-service test.
    gateway = os.environ.get("GATEWAY_URL", "http://localhost:8080")
    async with httpx.AsyncClient(base_url=gateway, timeout=15.0) as client:
        summary = await seed(client)
    print(f"seeded via {gateway}: {summary['created']} created, "
          f"{summary['replayed']} already present")


def main() -> None:  # pragma: no cover
    asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover
    main()

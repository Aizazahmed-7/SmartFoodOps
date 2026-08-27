"""Deterministic demo data through the REAL APIs (docs/local-dev.md §seeding).

Never raw SQL: every restaurant goes register → onboard (grant fires) →
refresh (claims arrive) → menu CRUD — so seeding exercises auth, validation,
versioning, and the outbox exactly like production traffic.

Idempotent by construction: onboarding replays return the existing
restaurant (200), and a restaurant that already has categories is skipped —
`make seed` twice is safe and changes nothing.

Every restaurant name is unique across BOTH cities: browse must never show
two rows with the same name (dupes read as broken data, and made real
duplicates from stray test runs impossible to spot).
"""

import asyncio
import os
from typing import Any, cast

import httpx
from smartfood_auth import internal_headers

PASSWORD = "demo1234demo"  # every demo login, per docs/local-dev.md

# ── the toy city (dispatch milestone) ──────────────────────────────
# REAL lat/lon over a DRAWN map: every restaurant and address gets a fixed
# coordinate inside a small bounding box per city, so Redis GEOSEARCH, the
# 3 km offer radius and haversine ETAs all run on genuine geography — while
# the frontend renders the box as its own 2D game map. Fake world, real math.
CITY_BOXES: dict[str, tuple[float, float, float, float]] = {
    # (south lat, west lon, north lat, east lon) — ~4.4 km × ~3.4 km
    "springfield": (39.780, -89.670, 39.820, -89.630),
    "shelbyville": (39.860, -89.670, 39.900, -89.630),
}


def city_coords(city: str, index: int) -> tuple[float, float]:
    """Deterministic spread: a 4-wide grid inset from the box edges, so
    ten restaurants land in distinct, stable, demo-legible spots."""
    south, west, north, east = CITY_BOXES[city]
    col, row = index % 4, index // 4
    lat = south + (north - south) * (0.18 + 0.28 * row)
    lon = west + (east - west) * (0.14 + 0.24 * col)
    return round(lat, 6), round(lon, 6)


# STRICT stock (Inventory, W2): items are born at 0 and cannot sell until
# stocked. Seed stocks every item so demo orders can actually validate.
SEED_STOCK = 100
SEED_CAPACITY = 20

# Three modifier shapes so the UI's every path has data:
# required radio (SIZE), optional radio (SPICE), multi-select (ADDONS).
SIZE = [
    {
        "name": "Size",
        "min_select": 1,
        "max_select": 1,
        "options": [
            {"name": "Regular", "rank": 0},
            {"name": "Large", "price_delta_cents": 300, "rank": 1},
        ],
    }
]
SPICE = [
    {
        "name": "Spice Level",
        "min_select": 0,
        "max_select": 1,
        "options": [
            {"name": "Mild", "rank": 0},
            {"name": "Medium", "rank": 1},
            {"name": "Extra Hot", "rank": 2},
        ],
    }
]
ADDONS = [
    {
        "name": "Add-ons",
        "min_select": 0,
        "max_select": 3,
        "options": [
            {"name": "Extra Cheese", "price_delta_cents": 150, "rank": 0},
            {"name": "Bacon", "price_delta_cents": 200, "rank": 1},
            {"name": "Avocado", "price_delta_cents": 250, "rank": 2},
        ],
    }
]

# city, name, cuisines, {category: [(item, cents, description, tags, groups)]}
TEMPLATES: list[dict[str, Any]] = [
    # ── springfield ──────────────────────────────────────────────────
    {
        "city": "springfield",
        "name": "Biryani House",
        "cuisines": ["pakistani", "bbq"],
        # The demo's multi-branch brand (ADR-0028): a second location whose
        # branch inherits the base menu; seeded with its own stock and one
        # branch-86'd base item for demo texture.
        "branches": [{"label": "Airport", "city": "springfield"}],
        "menu": {
            "Mains": [
                (
                    "Chicken Biryani",
                    1200,
                    "Fragrant basmati layered with spiced chicken and caramelized onions",
                    ["halal", "spicy"],
                    SIZE,
                ),
                (
                    "Mutton Karahi",
                    1800,
                    "Slow-cooked in a wok with ginger, tomatoes and green chilies",
                    ["halal"],
                    SPICE,
                ),
                (
                    "Seekh Kebab",
                    950,
                    "Char-grilled minced beef skewers with mint chutney",
                    ["halal"],
                    [],
                ),
            ],
            "Sides": [
                ("Raita", 200, "Cool yogurt with cucumber and roasted cumin", ["vegetarian"], []),
                (
                    "Garlic Naan",
                    250,
                    "Tandoor-baked flatbread brushed with garlic butter",
                    ["vegetarian"],
                    [],
                ),
            ],
        },
    },
    {
        "city": "springfield",
        "name": "Burger Barn",
        "cuisines": ["burgers", "fast-food"],
        "menu": {
            "Burgers": [
                (
                    "Smash Burger",
                    950,
                    "Double-smashed patties, American cheese, house sauce",
                    [],
                    SIZE + ADDONS,
                ),
                (
                    "Veggie Burger",
                    850,
                    "Black-bean patty with chipotle mayo",
                    ["vegetarian"],
                    ADDONS,
                ),
                (
                    "BBQ Bacon Burger",
                    1250,
                    "Smoked bacon, cheddar, crispy onions, bourbon BBQ",
                    [],
                    ADDONS,
                ),
            ],
            "Sides": [
                (
                    "Loaded Fries",
                    550,
                    "Cheese sauce, jalapenos, scallions",
                    ["vegetarian", "spicy"],
                    [],
                ),
                ("Onion Rings", 450, "Beer-battered, served with ranch", ["vegetarian"], []),
            ],
            "Shakes": [
                ("Vanilla Malt", 500, "Hand-spun with real vanilla bean", ["vegetarian"], []),
            ],
        },
    },
    {
        "city": "springfield",
        "name": "Pasta Palace",
        "cuisines": ["italian"],
        "menu": {
            "Pasta": [
                ("Carbonara", 1400, "Guanciale, pecorino, egg yolk — no cream, ever", [], []),
                (
                    "Arrabbiata",
                    1200,
                    "Penne in a fiery garlic-chili tomato sauce",
                    ["vegetarian", "spicy"],
                    [],
                ),
                ("Lasagna al Forno", 1500, "Layered beef ragu baked with bechamel", [], []),
            ],
            "Salads": [
                ("Caprese", 900, "Buffalo mozzarella, heirloom tomato, basil", ["vegetarian"], []),
            ],
            "Dolci": [
                ("Tiramisu", 700, "Espresso-soaked ladyfingers, mascarpone", ["vegetarian"], []),
            ],
        },
    },
    {
        "city": "springfield",
        "name": "Sushi Spot",
        "cuisines": ["japanese"],
        "menu": {
            "Rolls": [
                ("California Roll", 1100, "Crab, avocado, cucumber", [], []),
                ("Spicy Tuna", 1300, "Tuna, sriracha mayo, scallion", ["spicy"], []),
                ("Dragon Roll", 1500, "Eel and avocado over shrimp tempura", [], []),
            ],
            "Small Plates": [
                ("Miso Soup", 350, "Tofu, wakame, scallion", ["vegetarian"], []),
                ("Edamame", 400, "Steamed and sea-salted", ["vegan"], []),
            ],
        },
    },
    {
        "city": "springfield",
        "name": "Taco Town",
        "cuisines": ["mexican"],
        "menu": {
            "Tacos": [
                ("Al Pastor", 900, "Spit-roasted pork, pineapple, cilantro", ["spicy"], SPICE),
                ("Baja Fish", 1000, "Beer-battered cod, cabbage slaw, lime crema", [], []),
                (
                    "Veggie Taco",
                    800,
                    "Grilled peppers, black beans, avocado",
                    ["vegetarian", "vegan"],
                    [],
                ),
            ],
            "Burritos": [
                ("Carne Asada Burrito", 1150, "Grilled steak, rice, beans, salsa roja", [], SPICE),
            ],
            "Sides": [
                ("Chips and Guac", 600, "Fresh-mashed guacamole", ["vegan"], []),
            ],
        },
    },
    {
        "city": "springfield",
        "name": "Curry Corner",
        "cuisines": ["indian"],
        "menu": {
            "Curries": [
                (
                    "Butter Chicken",
                    1500,
                    "Tandoori chicken in velvety tomato-butter sauce",
                    [],
                    SIZE + SPICE,
                ),
                ("Chana Masala", 1100, "Chickpeas in tangy amchoor gravy", ["vegan"], SPICE),
                ("Palak Paneer", 1250, "House-made paneer in spinach puree", ["vegetarian"], []),
            ],
            "Breads": [
                ("Butter Naan", 250, "Blistered in the tandoor", ["vegetarian"], []),
                ("Tandoori Roti", 150, "Whole-wheat, brushed with ghee on request", ["vegan"], []),
            ],
        },
    },
    {
        "city": "springfield",
        "name": "Pho Real",
        "cuisines": ["vietnamese"],
        "menu": {
            "Soups": [
                ("Beef Pho", 1300, "Twelve-hour bone broth, brisket, rice noodles", [], SIZE),
                ("Tofu Pho", 1150, "Aromatic vegetable broth, fried tofu", ["vegetarian"], SIZE),
            ],
            "Street Snacks": [
                ("Fresh Spring Rolls", 550, "Shrimp, herbs, vermicelli, peanut dip", [], []),
                ("Banh Mi", 900, "Grilled pork, pickled daikon, pate, baguette", [], []),
            ],
        },
    },
    {
        "city": "springfield",
        "name": "Falafel Factory",
        "cuisines": ["middle-eastern"],
        "menu": {
            "Wraps": [
                (
                    "Falafel Wrap",
                    850,
                    "Crispy chickpea falafel, tahini, pickles",
                    ["vegan", "halal"],
                    [],
                ),
                ("Shawarma", 1000, "Marinated chicken off the spit, garlic toum", ["halal"], []),
            ],
            "Plates": [
                ("Mixed Grill Plate", 1600, "Kofta, shish tawook, saffron rice", ["halal"], []),
                ("Hummus Bowl", 700, "Silky hummus, olive oil, warm pita", ["vegan"], []),
            ],
        },
    },
    {
        "city": "springfield",
        "name": "Wok This Way",
        "cuisines": ["chinese"],
        "menu": {
            "Stir Fry": [
                (
                    "Kung Pao Chicken",
                    1250,
                    "Peanuts, dried chilies, Sichuan pepper",
                    ["spicy"],
                    SPICE,
                ),
                ("Beef and Broccoli", 1300, "Wok-seared in oyster sauce", [], []),
                ("Veg Chow Mein", 1000, "Springy noodles, seasonal greens", ["vegetarian"], []),
            ],
            "Rice": [
                ("Yangzhou Fried Rice", 900, "Shrimp, char siu, egg", [], []),
            ],
        },
    },
    {
        "city": "springfield",
        "name": "Pizza Planet",
        "cuisines": ["pizza", "italian"],
        "menu": {
            "Pizzas": [
                (
                    "Margherita",
                    1300,
                    "San Marzano tomato, fior di latte, basil",
                    ["vegetarian"],
                    SIZE,
                ),
                ("Pepperoni", 1500, "Cup-and-char pepperoni, hot honey drizzle", [], SIZE),
                (
                    "Truffle Mushroom",
                    1600,
                    "Roasted mushrooms, truffle cream, no tomato",
                    ["vegetarian"],
                    SIZE,
                ),
            ],
            "Sides": [
                (
                    "Garlic Knots",
                    450,
                    "Parmesan, parsley, roasted-garlic butter",
                    ["vegetarian"],
                    [],
                ),
                ("Buffalo Wings", 700, "Tossed in house buffalo sauce", ["spicy"], []),
            ],
        },
    },
    # ── shelbyville ──────────────────────────────────────────────────
    {
        "city": "shelbyville",
        "name": "Seoul Kitchen",
        "cuisines": ["korean"],
        "menu": {
            "Mains": [
                (
                    "Bibimbap",
                    1250,
                    "Stone-bowl rice, seasonal vegetables, gochujang, fried egg",
                    ["vegetarian"],
                    SPICE,
                ),
                ("Bulgogi Bowl", 1400, "Soy-pear marinated beef over steamed rice", [], []),
                ("Korean Fried Chicken", 1300, "Double-fried, gochujang glaze", ["spicy"], []),
            ],
            "Sides": [
                ("Kimchi", 300, "House-fermented napa cabbage", ["vegan", "spicy"], []),
            ],
        },
    },
    {
        "city": "shelbyville",
        "name": "Noodle Nirvana",
        "cuisines": ["thai"],
        "menu": {
            "Noodles": [
                ("Pad Thai", 1200, "Tamarind, peanuts, lime", [], SPICE),
                ("Drunken Noodles", 1250, "Wide rice noodles, Thai basil, chili", ["spicy"], SPICE),
            ],
            "Curries": [
                ("Green Curry", 1350, "Coconut milk, bamboo shoots, Thai eggplant", ["spicy"], []),
            ],
            "Desserts": [
                ("Mango Sticky Rice", 650, "Coconut cream, ripe mango", ["vegan"], []),
            ],
        },
    },
    {
        "city": "shelbyville",
        "name": "The Greek Corner",
        "cuisines": ["greek", "mediterranean"],
        "menu": {
            "Gyros": [
                ("Chicken Gyro", 950, "Spit-roasted, tzatziki, warm pita", [], []),
                ("Lamb Gyro", 1100, "Slow-roasted lamb, red onion, tomato", [], []),
            ],
            "Plates": [
                ("Greek Salad", 850, "Feta, kalamata olives, cucumber", ["vegetarian"], []),
                ("Moussaka", 1300, "Eggplant, spiced lamb, bechamel", [], []),
            ],
            "Sweets": [
                ("Baklava", 500, "Walnut, honey, phyllo", ["vegetarian"], []),
            ],
        },
    },
    {
        "city": "shelbyville",
        "name": "Bagel Bros",
        "cuisines": ["breakfast", "cafe"],
        "menu": {
            "Bagels": [
                (
                    "Lox and Schmear",
                    1200,
                    "Cured salmon, cream cheese, capers, everything bagel",
                    [],
                    [],
                ),
                ("Classic BEC", 750, "Bacon, egg, cheese on a plain bagel", [], []),
                ("Avocado Smash", 850, "Chili flakes, lemon, sesame bagel", ["vegetarian"], []),
            ],
            "Coffee": [
                ("Drip Coffee", 300, "Bottomless while you wait", ["vegan"], SIZE),
                ("Oat Latte", 550, "Double shot, oat milk", ["vegan"], SIZE),
            ],
        },
    },
    {
        "city": "shelbyville",
        "name": "Smoke Stack BBQ",
        "cuisines": ["bbq", "american"],
        "menu": {
            "Plates": [
                ("Brisket Plate", 1800, "Fourteen-hour smoked, two sides", [], []),
                ("Pulled Pork Sandwich", 1100, "Carolina vinegar sauce, slaw", [], []),
                ("Half Rack Ribs", 1600, "Dry-rubbed St. Louis cut", [], []),
            ],
            "Sides": [
                ("Mac and Cheese", 500, "Three-cheese, smoked", ["vegetarian"], []),
                ("Cornbread", 350, "Honey butter", ["vegetarian"], []),
            ],
        },
    },
    {
        "city": "shelbyville",
        "name": "Green Bowl",
        "cuisines": ["healthy", "salads"],
        "menu": {
            "Bowls": [
                ("Quinoa Power Bowl", 1150, "Roasted sweet potato, kale, tahini", ["vegan"], []),
                ("Kale Caesar", 1050, "Almond parm, sourdough croutons", ["vegetarian"], []),
                (
                    "Harvest Bowl",
                    1200,
                    "Wild rice, roasted squash, goat cheese",
                    ["vegetarian"],
                    [],
                ),
            ],
            "Juices": [
                ("Green Juice", 600, "Celery, apple, ginger, lemon", ["vegan"], []),
            ],
        },
    },
    {
        "city": "shelbyville",
        "name": "Dumpling Dynasty",
        "cuisines": ["chinese", "dim-sum"],
        "menu": {
            "Dumplings": [
                ("Pork Soup Dumplings", 950, "Eight per steamer, rich aspic broth", [], []),
                (
                    "Chive and Egg Dumplings",
                    850,
                    "Pan-fried, crispy lace bottom",
                    ["vegetarian"],
                    [],
                ),
                ("Har Gow", 800, "Crystal shrimp dumplings", [], []),
            ],
            "Small Plates": [
                ("Scallion Pancake", 550, "Flaky, with black-vinegar dip", ["vegan"], []),
                ("Dan Dan Noodles", 1000, "Sesame, chili oil, minced pork", ["spicy"], SPICE),
            ],
        },
    },
    {
        "city": "shelbyville",
        "name": "Cluck Truck",
        "cuisines": ["fried-chicken", "fast-food"],
        "menu": {
            "Sandwiches": [
                (
                    "Nashville Hot Sandwich",
                    1050,
                    "Cayenne-dredged, comeback sauce, pickles",
                    ["spicy"],
                    SPICE,
                ),
                ("Classic Crispy Sandwich", 950, "Buttermilk brine, mayo, slaw", [], ADDONS),
            ],
            "Tenders": [
                ("Tender Basket", 900, "Three jumbo tenders, Texas toast", [], SPICE),
            ],
            "Sides": [
                ("Waffle Fries", 450, "Crinkle-cut, seasoned salt", ["vegan"], []),
            ],
        },
    },
    {
        "city": "shelbyville",
        "name": "Bella Napoli",
        "cuisines": ["pizza", "italian"],
        "menu": {
            "Pizzas": [
                ("Napoletana", 1400, "Wood-fired, anchovies, capers, oregano", [], SIZE),
                ("Quattro Formaggi", 1550, "Four-cheese white pie", ["vegetarian"], SIZE),
                ("Diavola", 1500, "Spicy salami, Calabrian chili", ["spicy"], SIZE),
            ],
            "Antipasti": [
                ("Burrata", 950, "Cream-filled mozzarella, grilled bread", ["vegetarian"], []),
            ],
        },
    },
    {
        "city": "shelbyville",
        "name": "Sweet Tooth",
        "cuisines": ["desserts", "bakery"],
        "menu": {
            "Cakes": [
                ("Chocolate Fudge Slice", 650, "Triple-layer, dark ganache", ["vegetarian"], []),
                ("Basque Cheesecake", 700, "Burnt top, custard center", ["vegetarian"], []),
            ],
            "Cookies": [
                ("Brown Butter Chocolate Chip", 350, "Baked hourly, sea salt", ["vegetarian"], []),
            ],
            "Shakes": [
                (
                    "Strawberry Milkshake",
                    550,
                    "Fresh strawberries, whipped cream",
                    ["vegetarian"],
                    SIZE,
                ),
            ],
        },
    },
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
    client: httpx.AsyncClient, template: dict[str, Any], position: tuple[float, float]
) -> bool:
    """Returns True if created, False if it already existed (replay)."""
    city = template["city"]
    lat, lon = position
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
        json={
            "name": template["name"],
            "city": city,
            "cuisines": template["cuisines"],
            "lat": lat,
            "lon": lon,
        },
        headers=bearer,
    )
    restaurant = _expect(onboarded, 200, 201)
    restaurant_id = restaurant["id"]  # the BRAND — menu CRUD edits the base menu
    # The first branch is the physical location: stock, capacity and the
    # map pin belong to it (ADR-0028).
    branch_id = restaurant["branches"][0]["id"]

    # Refresh: the rotation carries the restaurant_admin grant into the claims.
    fresh = _expect(
        await client.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}),
        200,
    )
    admin = {"Authorization": f"Bearer {fresh['access_token']}"}

    if restaurant["branches"][0].get("lat") is None:  # pragma: no cover — legacy-volume
        # upgrade, exercised by the live seed against a pre-dispatch volume
        # (the test world is always born WITH coordinates). Backfill exactly
        # once, onto the BRANCH — dispatch reads the branch pin.
        _expect(
            await client.patch(
                f"/v1/restaurants/{branch_id}", json={"lat": lat, "lon": lon}, headers=admin
            ),
            200,
        )

    menu = _expect(await client.get(f"/v1/menus/{restaurant_id}"), 200)
    if menu["categories"]:
        # Already seeded — replays change nothing the admin may have touched;
        # stock is only topped up where it is verifiably untouched (0 @ v0).
        # EVERY branch gets the top-up, each from its own effective menu:
        # base items AND any branch-local additions made since (ADR-0028).
        for existing_branch in restaurant["branches"]:
            b_menu = _expect(await client.get(f"/v1/menus/{existing_branch['id']}"), 200)
            await _ensure_stock(client, admin, existing_branch["id"], b_menu)
        await _seed_branches(client, admin, restaurant_id, template)
        return False

    created_item_ids: list[str] = []
    for rank, (category_name, items) in enumerate(template["menu"].items()):
        category = _expect(
            await client.post(
                f"/v1/restaurants/{restaurant_id}/categories",
                json={"name": category_name, "rank": rank},
                headers=admin,
            ),
            201,
        )
        for item_rank, (item_name, price_cents, description, tags, groups) in enumerate(items):
            item = _expect(
                await client.post(
                    f"/v1/restaurants/{restaurant_id}/items",
                    json={
                        "category_id": category["id"],
                        "name": item_name,
                        "price_cents": price_cents,
                        "description": description,
                        "rank": item_rank,
                        "tags": tags,
                        "modifier_groups": groups,
                    },
                    headers=admin,
                ),
                201,
            )
            created_item_ids.append(item["id"])

    for item_id in created_item_ids:
        _expect(
            await client.put(
                f"/v1/inventory/restaurants/{branch_id}/stock/{item_id}",
                json={"available": SEED_STOCK},
                headers=admin,
            ),
            200,
        )
    _expect(
        await client.put(
            f"/v1/inventory/restaurants/{branch_id}/capacity",
            json={"capacity": SEED_CAPACITY},
            headers=admin,
        ),
        200,
    )
    await _seed_branches(client, admin, restaurant_id, template)
    return True


async def _seed_branches(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    brand_id: str,
    template: dict[str, Any],
) -> None:
    """Extra locations for multi-branch templates (ADR-0028). Idempotent:
    branch create replays 200 by label; stock only tops up untouched rows;
    the demo's branch-86 (one base item off at the extra branch — texture
    for the inheritance story) is asserted only on first creation, so an
    admin's later restore survives re-seeding."""
    for offset, spec in enumerate(template.get("branches", [])):
        # Grid slots 10+ sit below the ten template pins — distinct and stable.
        lat, lon = city_coords(spec["city"], 10 + offset)
        response = await client.post(
            f"/v1/restaurants/{brand_id}/branches",
            json={"branch_label": spec["label"], "city": spec["city"], "lat": lat, "lon": lon},
            headers=admin,
        )
        branch = _expect(response, 200, 201)
        if response.status_code == 201:  # replays were topped up by the caller
            branch_menu = _expect(await client.get(f"/v1/menus/{branch['id']}"), 200)
            await _ensure_stock(client, admin, branch["id"], branch_menu)
            _expect(
                await client.put(
                    f"/v1/inventory/restaurants/{branch['id']}/capacity",
                    json={"capacity": SEED_CAPACITY},
                    headers=admin,
                ),
                200,
            )
            first_item = branch_menu["categories"][0]["items"][0]["id"]
            _expect(
                await client.put(
                    f"/v1/restaurants/{branch['id']}/base-items/{first_item}/availability",
                    json={"available": False},
                    headers=admin,
                ),
                200,
            )


async def _ensure_stock(
    client: httpx.AsyncClient, admin: dict[str, str], restaurant_id: str, menu: dict[str, Any]
) -> None:
    """Replay path: stock only items that are verifiably untouched
    (available 0 at version 0 — never PUT by an admin, or missing entirely).
    An admin's counts and capacity survive re-seeding."""
    current = _expect(
        await client.get(f"/v1/inventory/restaurants/{restaurant_id}/stock", headers=admin),
        200,
    )
    by_id = {row["item_id"]: row for row in current["items"]}
    menu_item_ids = [item["id"] for category in menu["categories"] for item in category["items"]]
    for item_id in menu_item_ids:
        row = by_id.get(item_id)
        if row is None or (row["available"] == 0 and row["version"] == 0):
            _expect(
                await client.put(
                    f"/v1/inventory/restaurants/{restaurant_id}/stock/{item_id}",
                    json={"available": SEED_STOCK},
                    headers=admin,
                ),
                200,
            )


DEMO_CUSTOMER = "customer@demo.smartfood.dev"
# Home sits mid-box, a couple of blocks off the restaurant grid — every
# demo delivery has a real, visible drive.
DEMO_ADDRESS = {
    "label": "home",
    "line1": "12 Mango St",
    "city": "springfield",
    "lat": 39.8025,
    "lon": -89.6478,
}

# The demo couriers (dispatch milestone). Registered like any customer,
# then promoted through identity's internal grant — the same two-step a
# future self-serve rider onboarding would use.
DEMO_RIDERS = [f"rider{i}@demo.smartfood.dev" for i in (1, 2, 3)]


async def _seed_customer(client: httpx.AsyncClient) -> bool:
    """The demo customer every walkthrough logs in as (S9): registered,
    with one saved address — the placement API requires an address_id.
    Returns True if the address was created this run (replay = False)."""
    await client.post(
        "/v1/auth/register", json={"email": DEMO_CUSTOMER, "password": PASSWORD}
    )  # idempotent no-op if present; login below is the real gate
    pair = _expect(
        await client.post("/v1/auth/login", json={"email": DEMO_CUSTOMER, "password": PASSWORD}),
        200,
    )
    bearer = {"Authorization": f"Bearer {pair['access_token']}"}
    # _expect is typed for object envelopes; this endpoint returns an array.
    addresses = cast(
        "list[dict[str, Any]]", _expect(await client.get("/v1/me/addresses", headers=bearer), 200)
    )
    home = next((a for a in addresses if a["label"] == DEMO_ADDRESS["label"]), None)
    if home is not None and home.get("lat") is not None:
        return False
    if home is not None:  # pragma: no cover — the same legacy-volume upgrade
        # as the restaurant backfill above, proven by the live seed run.
        # A coordless pre-dispatch address cannot anchor a delivery on the
        # map — replace it (delete+create; the id changes, nothing stores it).
        _expect(await client.delete(f"/v1/me/addresses/{home['id']}", headers=bearer), 204, 200)
    _expect(await client.post("/v1/me/addresses", json=DEMO_ADDRESS, headers=bearer), 201)
    return True


async def _seed_riders(client: httpx.AsyncClient, identity_base_url: str) -> int:
    """Register + promote the demo couriers. The grant is an INTERNAL
    identity endpoint (system-authed, never edge-routed), so this is the
    seed's one absolute-URL call — the same trust boundary catalog's
    onboarding grant crosses. Idempotent: a granted rider replays 200."""
    granted = 0
    for email in DEMO_RIDERS:
        await client.post("/v1/auth/register", json={"email": email, "password": PASSWORD})
        pair = _expect(
            await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD}), 200
        )
        bearer = {"Authorization": f"Bearer {pair['access_token']}"}
        me = _expect(await client.get("/v1/auth/me", headers=bearer), 200)
        if me["role"] == "rider":
            continue  # replay — already promoted
        _expect(
            await client.post(
                f"{identity_base_url}/v1/internal/grants",
                json={"user_id": me["id"], "role": "rider"},
                headers=internal_headers("seed"),
            ),
            200,
        )
        granted += 1
    return granted


async def seed(
    client: httpx.AsyncClient, *, identity_base_url: str = "http://localhost:8001"
) -> dict[str, int]:
    created = replayed = 0
    position: dict[str, int] = {}  # per-city grid index, template order = stable spots
    for template in TEMPLATES:
        index = position.setdefault(template["city"], 0)
        position[template["city"]] = index + 1
        if await _seed_restaurant(client, template, city_coords(template["city"], index)):
            created += 1
        else:
            replayed += 1
    await _seed_customer(client)
    riders = await _seed_riders(client, identity_base_url)
    return {"created": created, "replayed": replayed, "riders_granted": riders}


async def _amain() -> None:  # pragma: no cover — entrypoint wiring; the seed()
    # flow itself is fully covered by the in-process two-service test.
    gateway = os.environ.get("GATEWAY_URL", "http://localhost:8080")
    identity = os.environ.get("IDENTITY_BASE_URL", "http://localhost:8001")
    async with httpx.AsyncClient(base_url=gateway, timeout=15.0) as client:
        summary = await seed(client, identity_base_url=identity)
    print(
        f"seeded via {gateway}: {summary['created']} created, {summary['replayed']} already present"
    )


def main() -> None:  # pragma: no cover
    asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover
    main()

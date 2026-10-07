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
    # (south lat, west lon, north lat, east lon) — ~4.4 km × ~3.4 km each.
    # Real coordinates: Islamabad's F-sectors and Rawalpindi's Saddar, about
    # 12 km apart, which is what makes the 3 km offer radius and the
    # haversine ETAs behave like the real thing rather than like a diagram.
    # The city ids match `frontend/src/cities.ts` exactly — they did not
    # before, and a seeded world nobody could reach from the city chips was
    # the result.
    "islamabad": (33.690, 73.030, 33.730, 73.067),
    "rawalpindi": (33.580, 73.040, 33.620, 73.077),
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
    # ── islamabad ────────────────────────────────────────────────────
    {
        "city": "islamabad",
        "name": "Biryani House",
        "cuisines": ["pakistani", "bbq"],
        # The demo's multi-branch brand (ADR-0028): a second location whose
        # branch inherits the base menu; seeded with its own stock and one
        # branch-86'd base item for demo texture. The branch sits in the
        # OTHER city on purpose — a brand spanning Islamabad and Rawalpindi
        # is what makes the city filter visibly do something.
        "branches": [{"label": "Saddar", "city": "rawalpindi"}],
        "menu": {
            "Mains": [
                (
                    "Chicken Biryani",
                    450,
                    "Fragrant basmati layered with spiced chicken and caramelised onions",
                    ["halal", "spicy"],
                    SIZE,
                ),
                (
                    "Mutton Karahi",
                    1850,
                    "Slow-cooked in a wok with ginger, tomatoes and green chillies",
                    ["halal"],
                    SPICE,
                ),
                (
                    "Seekh Kebab",
                    380,
                    "Char-grilled minced beef skewers with mint chutney",
                    ["halal"],
                    [],
                ),
            ],
            "Sides": [
                (
                    "Raita",
                    120,
                    "Cool yoghurt with cucumber and roasted cumin",
                    ["vegetarian"],
                    [],
                ),
                (
                    "Garlic Naan",
                    90,
                    "Tandoor-baked flatbread brushed with garlic butter",
                    ["vegetarian"],
                    [],
                ),
            ],
        },
    },
    {
        "city": "islamabad",
        "name": "Savour Pulao",
        "cuisines": ["pakistani"],
        "menu": {
            "Pulao": [
                (
                    "Chicken Pulao",
                    380,
                    "Rice steamed in seasoned stock with tender chicken",
                    ["halal"],
                    SIZE,
                ),
                ("Mutton Pulao", 520, "The same pot, with slow-cooked mutton", ["halal"], SIZE),
            ],
            "Sides": [
                ("Shami Kebab", 150, "Griddled lentil and beef patty", ["halal"], []),
                ("Kachumber Salad", 80, "Onion, tomato and cucumber with lemon", ["vegan"], []),
            ],
        },
    },
    {
        "city": "islamabad",
        "name": "Kabul Grill",
        "cuisines": ["middle-eastern", "bbq"],
        "menu": {
            "Grill": [
                (
                    "Chapli Kebab",
                    420,
                    "Flat spiced beef patty fried with tomato and coriander",
                    ["halal", "spicy"],
                    SPICE,
                ),
                ("Afghani Tikka", 480, "Yoghurt-marinated chicken over coals", ["halal"], SIZE),
            ],
            "Breads": [
                ("Afghani Naan", 100, "Long tandoor bread, sesame crusted", ["vegetarian"], []),
            ],
        },
    },
    {
        "city": "islamabad",
        "name": "Tuscany Courtyard",
        "cuisines": ["italian"],
        "menu": {
            "Pasta": [
                (
                    "Chicken Alfredo",
                    550,
                    "Fettuccine in a cream and parmesan sauce",
                    ["halal"],
                    SIZE,
                ),
                (
                    "Arrabbiata",
                    460,
                    "Penne in a chilli and garlic tomato sauce",
                    ["vegetarian", "spicy"],
                    SPICE,
                ),
            ],
            "Sides": [("Garlic Bread", 180, "Baked with herb butter", ["vegetarian"], ADDONS)],
        },
    },
    {
        "city": "islamabad",
        "name": "Sakura Teppan",
        "cuisines": ["japanese"],
        "menu": {
            "Sushi": [
                ("Salmon Maki", 680, "Eight pieces, rolled to order", [], SIZE),
                ("Avocado Maki", 520, "Eight pieces, no fish", ["vegan"], SIZE),
            ],
            "Hot": [
                ("Chicken Katsu", 620, "Panko-crumbed cutlet with tonkatsu sauce", ["halal"], []),
            ],
        },
    },
    {
        "city": "islamabad",
        "name": "Taco Bandido",
        "cuisines": ["mexican"],
        "menu": {
            "Tacos": [
                ("Beef Birria Tacos", 490, "Three tacos with consommé to dip", ["halal"], SPICE),
                ("Bean Tacos", 390, "Three tacos, black bean and lime", ["vegan"], SPICE),
            ],
            "Sides": [("Loaded Nachos", 320, "Cheese, jalapeño, salsa", ["vegetarian"], ADDONS)],
        },
    },
    {
        "city": "islamabad",
        "name": "Delhi Darbar",
        "cuisines": ["indian"],
        "menu": {
            "Curry": [
                (
                    "Butter Chicken",
                    520,
                    "Tomato and cream, finished with butter",
                    ["halal"],
                    SIZE,
                ),
                (
                    "Palak Paneer",
                    440,
                    "Spinach with cubes of fresh cheese",
                    ["vegetarian"],
                    SIZE,
                ),
            ],
            "Breads": [
                ("Butter Naan", 90, "Tandoor-baked, brushed with butter", ["vegetarian"], [])
            ],
        },
    },
    {
        "city": "islamabad",
        "name": "Saigon Bowl",
        "cuisines": ["vietnamese"],
        "menu": {
            "Pho": [
                ("Beef Pho", 540, "Twelve-hour bone broth, rice noodles, herbs", ["halal"], SIZE),
                ("Veg Pho", 440, "Mushroom broth, tofu, herbs", ["vegan"], SIZE),
            ],
            "Rolls": [("Summer Rolls", 280, "Rice paper, herbs, peanut dip", ["vegan"], [])],
        },
    },
    {
        "city": "islamabad",
        "name": "Golden Dragon",
        "cuisines": ["chinese"],
        "menu": {
            "Wok": [
                ("Chicken Manchurian", 470, "Crisp chicken in a tangy gravy", ["halal"], SPICE),
                ("Vegetable Chow Mein", 380, "Wok-tossed noodles and greens", ["vegan"], SIZE),
            ],
            "Soup": [("Hot and Sour Soup", 220, "Peppery, thickened, with egg", [], SPICE)],
        },
    },
    {
        "city": "islamabad",
        "name": "Pizza Pilgrim",
        "cuisines": ["pizza"],
        "menu": {
            "Pizza": [
                # The one dish with a required radio AND a multi-select, so
                # the checkbox path in the item modal has data (the old
                # world's "Burger Barn" smash played this part).
                ("Margherita", 480, "Tomato, mozzarella, basil", ["vegetarian"], SIZE + ADDONS),
                (
                    "Chicken Tikka Pizza",
                    620,
                    "The local favourite, on a thin base",
                    ["halal", "spicy"],
                    SIZE,
                ),
            ],
            "Sides": [("Cheesy Sticks", 240, "Baked with oregano", ["vegetarian"], ADDONS)],
        },
    },
    # ── rawalpindi ───────────────────────────────────────────────────
    {
        "city": "rawalpindi",
        "name": "Saddar Nihari",
        "cuisines": ["pakistani"],
        "menu": {
            "Nihari": [
                (
                    "Beef Nihari",
                    480,
                    "Overnight stew with ginger, chilli and fried onion",
                    ["halal", "spicy"],
                    SPICE,
                ),
                ("Maghaz Masala", 550, "Brain masala, a Rawalpindi breakfast", ["halal"], []),
            ],
            "Breads": [("Khameeri Roti", 70, "Leavened tandoor bread", ["vegetarian"], [])],
        },
    },
    {
        "city": "rawalpindi",
        "name": "Chatkhara Chaat",
        "cuisines": ["pakistani"],
        "menu": {
            "Chaat": [
                (
                    "Dahi Bhalla",
                    180,
                    "Lentil dumplings in yoghurt and tamarind",
                    ["vegetarian"],
                    [],
                ),
                ("Fruit Chaat", 160, "Seasonal fruit with chaat masala", ["vegan"], SIZE),
            ],
            "Snacks": [
                (
                    "Samosa Plate",
                    120,
                    "Four potato samosas with chutney",
                    ["vegetarian", "spicy"],
                    [],
                )
            ],
        },
    },
    {
        "city": "rawalpindi",
        "name": "Burger Adda",
        "cuisines": ["burgers", "fast-food"],
        "menu": {
            "Burgers": [
                (
                    "Zinger Burger",
                    320,
                    "Crumbed chicken fillet, mayo, lettuce",
                    ["halal"],
                    ADDONS,
                ),
                ("Beef Smash", 390, "Two smashed patties with cheese", ["halal"], ADDONS),
            ],
            "Sides": [
                (
                    "Masala Fries",
                    150,
                    "Fries tossed in chaat masala",
                    ["vegetarian", "spicy"],
                    SIZE,
                )
            ],
        },
    },
    {
        "city": "rawalpindi",
        "name": "Bombay Chowk",
        "cuisines": ["indian"],
        "menu": {
            "Curry": [
                (
                    "Chana Masala",
                    360,
                    "Chickpeas stewed with tomato and spice",
                    ["vegan", "spicy"],
                    SIZE,
                ),
                ("Rogan Josh", 580, "Kashmiri lamb curry", ["halal"], SPICE),
            ],
            "Rice": [("Jeera Rice", 180, "Basmati tempered with cumin", ["vegan"], [])],
        },
    },
    {
        "city": "rawalpindi",
        "name": "Noodle Bar Saddar",
        "cuisines": ["chinese"],
        "menu": {
            "Noodles": [
                (
                    "Chilli Garlic Noodles",
                    350,
                    "Wok-fried, heavy on the garlic",
                    ["vegan", "spicy"],
                    SPICE,
                ),
                ("Beef Chowmein", 440, "Thin noodles with strips of beef", ["halal"], SIZE),
            ],
            "Sides": [("Spring Rolls", 200, "Six, with sweet chilli dip", ["vegetarian"], [])],
        },
    },
    {
        "city": "rawalpindi",
        "name": "Napoli Slice",
        "cuisines": ["pizza", "italian"],
        "menu": {
            "Pizza": [
                ("Pepperoni", 580, "Beef pepperoni, mozzarella", ["halal"], SIZE),
                ("Veggie Supreme", 500, "Peppers, olives, mushroom, onion", ["vegetarian"], SIZE),
            ],
            "Pasta": [("Lasagne", 520, "Layered beef ragù and béchamel", ["halal"], [])],
        },
    },
    {
        "city": "rawalpindi",
        "name": "Sushi Saddar",
        "cuisines": ["japanese"],
        "menu": {
            "Sushi": [
                ("Tuna Nigiri", 640, "Four pieces over seasoned rice", [], SIZE),
                ("Cucumber Roll", 420, "Six pieces, simple and cold", ["vegan"], SIZE),
            ],
            "Hot": [("Chicken Ramen", 560, "Shoyu broth, soft egg, chashu", ["halal"], SPICE)],
        },
    },
    {
        "city": "rawalpindi",
        "name": "El Mariachi",
        "cuisines": ["mexican"],
        "menu": {
            "Mains": [
                (
                    "Chicken Quesadilla",
                    440,
                    "Griddled tortilla, cheese, peppers",
                    ["halal"],
                    ADDONS,
                ),
                ("Veggie Burrito", 400, "Rice, beans, salsa, wrapped", ["vegan"], SIZE),
            ],
            "Sides": [("Guacamole and Chips", 260, "Made to order", ["vegan"], [])],
        },
    },
    {
        "city": "rawalpindi",
        "name": "Pho Pindi",
        "cuisines": ["vietnamese"],
        "menu": {
            "Pho": [
                ("Chicken Pho", 460, "Clear broth, rice noodles, spring onion", ["halal"], SIZE),
                ("Tofu Pho", 400, "Mushroom broth with silken tofu", ["vegan"], SIZE),
            ],
            "Sides": [("Banh Mi", 300, "Baguette with pickled vegetables", ["halal"], ADDONS)],
        },
    },
    {
        "city": "rawalpindi",
        "name": "Zaitoon Shawarma",
        "cuisines": ["middle-eastern"],
        "menu": {
            "Wraps": [
                ("Chicken Shawarma", 280, "Carved off the spit, garlic sauce", ["halal"], ADDONS),
                ("Falafel Wrap", 240, "Fried chickpea patties, tahini", ["vegan"], ADDONS),
            ],
            "Plates": [("Hummus Plate", 220, "With warm pita and olive oil", ["vegan"], [])],
        },
    },
]
CITIES = ["islamabad", "rawalpindi"]


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
    """Replay path: stock items that are missing, or sitting at zero.

    This used to read `version == 0` to mean "never PUT by an admin", which
    told it apart from an admin who had deliberately zeroed a row. ADR-0039
    removed every `version` column, so that distinction no longer exists and
    the seed crashed on the missing key — caught by the first `make seed`
    after the Part A hardening pass.

    What survives re-seeding is therefore any NON-ZERO count, which is the
    case that matters: an admin's stock levels and capacity are left alone.
    What no longer survives is a deliberate 86 — a re-seed restocks it. That
    is a dev tool restoring a demo world, and the alternative is a seed that
    cannot run at all.
    """
    current = _expect(
        await client.get(f"/v1/inventory/restaurants/{restaurant_id}/stock", headers=admin),
        200,
    )
    by_id = {row["item_id"]: row for row in current["items"]}
    menu_item_ids = [item["id"] for category in menu["categories"] for item in category["items"]]
    for item_id in menu_item_ids:
        row = by_id.get(item_id)
        if row is None or row["available"] == 0:
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
    "line1": "House 12, Street 8, F-7/3",
    "city": "islamabad",
    "lat": 33.7105,
    "lon": 73.0512,
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
        if "rider" in me["roles"]:
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

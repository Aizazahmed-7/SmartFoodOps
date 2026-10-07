"""Chunking (ADR-0033). Pure function, so every branch is reachable here.

The assertions worth reading are the ones about what does NOT change: a
price edit, a pause, an 86'd dish and a reordered tag list must all leave
`content_hash` alone, because that hash is the only thing standing between
a routine kitchen afternoon and a provider bill.
"""

from ai_assistant.domain.knowledge import MAX_DESCRIPTION_CHARS, chunk

BRANCH = {
    "name": "Biryani House",
    "branch_label": "Downtown",
    "city": "springfield",
    "cuisines": ["Pakistani", "BBQ"],
    "status": "open",
    "kind": "branch",
    "brand_id": "b1",
    "menu": {
        "categories": [
            {
                "id": "c1",
                "name": "Mains",
                "items": [
                    {
                        "id": "i1",
                        "name": "Chicken Karahi",
                        "description": "Wok-cooked chicken with tomatoes.",
                        "price_cents": 899,
                        "available": True,
                        "tags": ["Spicy", "Halal"],
                    }
                ],
            }
        ]
    },
}


def _payload(**overrides):
    return {**BRANCH, **overrides}


def _chunked(**overrides):
    knowledge = chunk(restaurant_id="r1", payload=_payload(**overrides))
    assert knowledge is not None
    return knowledge


# ── what gets indexed at all ────────────────────────────────────────


def test_brand_payloads_are_not_chunked():
    """Brands are menu TEMPLATES, not places (ADR-0028). Skipping them is
    what keeps template rows out of results by construction rather than by a
    filter someone has to remember (FR-63)."""
    assert chunk(restaurant_id="b1", payload=_payload(kind="brand")) is None


def test_a_branch_without_a_city_is_not_chunked():
    """Every query is geo-scoped, so an unplaceable chunk is a row no
    predicate can reach and no operator can explain."""
    assert chunk(restaurant_id="r1", payload=_payload(city=None)) is None
    assert chunk(restaurant_id="r1", payload=_payload(city="   ")) is None


def test_items_without_an_id_are_skipped_not_fatal():
    """One malformed item must not cost the restaurant its whole menu."""
    good = BRANCH["menu"]["categories"][0]["items"][0]
    knowledge = _chunked(
        menu={"categories": [{"name": "Mains", "items": [{"name": "ghost"}, good]}]}
    )
    assert [i.item_id for i in knowledge.items] == ["i1"]


def test_menus_of_every_empty_shape_produce_just_the_restaurant_chunk():
    for menu in ({}, {"categories": []}, {"categories": None}, None, "nonsense"):
        knowledge = _chunked(menu=menu)
        assert knowledge.items == []
        assert knowledge.restaurant.id == "r1:_self"


def test_a_category_with_no_items_contributes_nothing():
    assert _chunked(menu={"categories": [{"name": "Empty", "items": None}]}).items == []


# ── identity ────────────────────────────────────────────────────────


def test_chunk_ids_are_derived_never_minted():
    """Deterministic ids are what make the consumer's NATURAL_KEY dedupe
    work under at-least-once delivery (DoD-2)."""
    first, second = _chunked(), _chunked()
    assert first.restaurant.id == second.restaurant.id == "r1:_self"
    assert [i.id for i in first.items] == [i.id for i in second.items] == ["r1:i1"]


def test_the_restaurant_chunk_cannot_collide_with_an_item():
    ids = {_chunked().restaurant.id} | {i.id for i in _chunked().items}
    assert len(ids) == 2


# ── the text recipe ─────────────────────────────────────────────────


def test_item_text_carries_the_durable_fields_and_nothing_volatile():
    item = _chunked().items[0]
    assert "Chicken Karahi" in item.content
    assert "Wok-cooked chicken" in item.content
    assert "Category: Mains" in item.content
    assert "Tags: halal, spicy" in item.content
    assert "Cuisines: bbq, pakistani" in item.content
    # FR-60: nothing that changes faster than an embedding can be refreshed.
    assert "899" not in item.content
    assert "available" not in item.content.lower()
    assert "open" not in item.content.lower()


def test_item_text_omits_the_restaurant_name():
    """Load-bearing for cost: it is what lets one embedding serve a base
    item inherited by every branch of a brand."""
    assert "Biryani House" not in _chunked().items[0].content


def test_the_same_item_under_two_branches_hashes_identically():
    """The ADR-0028 fan-out in miniature — two branches, different names and
    labels, one shared base dish, one embedding."""
    downtown = chunk(restaurant_id="r1", payload=_payload())
    airport = chunk(
        restaurant_id="r2", payload=_payload(name="Biryani House", branch_label="Airport")
    )
    assert downtown is not None and airport is not None
    assert downtown.items[0].content_hash == airport.items[0].content_hash
    # ...while the restaurants themselves stay distinguishable.
    assert downtown.restaurant.content_hash != airport.restaurant.content_hash


def test_restaurant_text_is_identity_not_location():
    content = _chunked().restaurant.content
    assert "Biryani House — Downtown" in content
    assert "Cuisines: bbq, pakistani" in content
    assert "springfield" not in content  # a hard predicate, not prose


def test_a_branch_with_no_label_uses_the_bare_name():
    assert _chunked(branch_label=None).restaurant.content.startswith("Biryani House\n")


def test_optional_text_lines_are_omitted_not_blank():
    knowledge = _chunked(
        cuisines=[],
        menu={
            "categories": [{"name": "Mains", "items": [{"id": "i1", "name": "Plain", "tags": []}]}]
        },
    )
    assert knowledge.restaurant.content == "Biryani House — Downtown"
    assert knowledge.items[0].content == "Plain\nCategory: Mains"


def test_long_descriptions_are_truncated_where_they_are_hashed():
    """Truncating here rather than at the provider keeps the text that was
    embedded identical to the text that was stored."""
    item = _chunked(
        menu={
            "categories": [
                {
                    "name": "Mains",
                    "items": [{"id": "i1", "name": "Epic", "description": "x" * 5000}],
                }
            ]
        }
    ).items[0]
    assert item.content.count("x") == MAX_DESCRIPTION_CHARS


# ── hash stability: the whole economy of the pipeline ────────────────


def test_a_price_change_does_not_change_the_hash():
    before = _chunked().items[0]
    after = _chunked(
        menu={
            "categories": [
                {
                    "name": "Mains",
                    "items": [{**BRANCH["menu"]["categories"][0]["items"][0], "price_cents": 1299}],
                }
            ]
        }
    ).items[0]
    assert before.content_hash == after.content_hash
    assert (before.price_cents, after.price_cents) == (899, 1299)


def test_an_86d_dish_stays_indexed_and_keeps_its_vector():
    """It comes back tomorrow. Deleting it would mean paying to re-embed
    unchanged text every time a kitchen runs out of chicken."""
    after = _chunked(
        menu={
            "categories": [
                {
                    "name": "Mains",
                    "items": [{**BRANCH["menu"]["categories"][0]["items"][0], "available": False}],
                }
            ]
        }
    ).items[0]
    assert after.available is False
    assert after.content_hash == _chunked().items[0].content_hash


def test_a_pause_does_not_change_either_hash():
    paused = _chunked(status="paused")
    assert paused.restaurant.status == "paused"
    assert paused.items[0].status == "paused"
    assert paused.restaurant.content_hash == _chunked().restaurant.content_hash
    assert paused.items[0].content_hash == _chunked().items[0].content_hash


def test_reordered_tags_do_not_change_the_hash():
    """Catalog returns tags in row order. Unsorted, a join that came back
    the other way round would rewrite every vector in the restaurant."""
    reordered = _chunked(
        menu={
            "categories": [
                {
                    "name": "Mains",
                    "items": [
                        {
                            **BRANCH["menu"]["categories"][0]["items"][0],
                            "tags": ["halal", "SPICY", "halal"],
                        }
                    ],
                }
            ]
        }
    ).items[0]
    assert reordered.tags == ["halal", "spicy"]
    assert reordered.content_hash == _chunked().items[0].content_hash


def test_reformatted_whitespace_does_not_change_the_hash():
    reformatted = _chunked(
        menu={
            "categories": [
                {
                    "name": " Mains ",
                    "items": [
                        {
                            **BRANCH["menu"]["categories"][0]["items"][0],
                            "name": "Chicken   Karahi",
                            "description": "Wok-cooked chicken\n  with tomatoes.",
                        }
                    ],
                }
            ]
        }
    ).items[0]
    assert reformatted.content_hash == _chunked().items[0].content_hash


def test_a_real_edit_does_change_the_hash():
    """The control case: without this, every assertion above is satisfied by
    a function that ignores its input."""
    edited = _chunked(
        menu={
            "categories": [
                {
                    "name": "Mains",
                    "items": [
                        {
                            **BRANCH["menu"]["categories"][0]["items"][0],
                            "description": "Now with extra chilli.",
                        }
                    ],
                }
            ]
        }
    ).items[0]
    assert edited.content_hash != _chunked().items[0].content_hash


# ── field plumbing ──────────────────────────────────────────────────


def test_scope_fields_are_denormalised_onto_every_item():
    """So the item leg needs no join inside a post-filtered ANN walk."""
    item = _chunked().items[0]
    assert (item.city, item.brand_id, item.status) == ("springfield", "b1", "open")
    assert item.cuisines == ["bbq", "pakistani"]


def test_a_legacy_branch_without_a_brand_keeps_a_null_brand_id():
    assert _chunked(brand_id=None).items[0].brand_id is None


def test_missing_status_defaults_to_open():
    """A restaurant that is serving orders and missing from search is the
    worse failure."""
    assert _chunked(status=None).restaurant.status == "open"


def test_a_missing_price_is_zero_not_a_crash():
    item = _chunked(
        menu={"categories": [{"name": "Mains", "items": [{"id": "i1", "name": "Free"}]}]}
    ).items[0]
    assert (item.price_cents, item.available) == (0, False)


def test_non_list_tag_payloads_degrade_to_empty():
    assert _chunked(cuisines="pakistani").restaurant.cuisines == []

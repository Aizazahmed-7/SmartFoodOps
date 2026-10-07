"""Catalog payload -> chunks. Pure, total, and the only place the text
recipe lives (ADR-0033).

Pure on purpose: no clock, no I/O, no randomness. Everything this milestone
can get subtly wrong — which facts are durable, which restaurants are
candidates, whether a replay re-embeds — is decidable from the payload
alone, so it is decidable in a unit test with no infrastructure at all.

Three properties the rest of the pipeline depends on:

1. **Deterministic ids.** `{restaurant_id}:{item_id}` and
   `{restaurant_id}:_self`. Derived, never minted, which is what makes the
   consumer's NATURAL_KEY dedupe work under at-least-once delivery (DoD-2).
2. **Stable text.** The same menu state always produces byte-identical
   `content`, so `content_hash` only changes when the MEANING changed.
   Tags and cuisines are sorted for exactly this reason: catalog returns
   them in row order, and an unsorted join would rewrite every vector in a
   restaurant because two tags swapped places.
3. **Durable text only.** Price, availability and status are excluded from
   `content` (FR-60) and carried as fields, so the routine churn of a
   kitchen — 86'ing a dish, a price rise, a pause — costs zero embeddings.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from .ports import ItemChunk, RestaurantChunk

SELF_SUFFIX = "_self"
"""The restaurant chunk's id suffix. Item ids are uuids, so this cannot
collide — which matters because `hashes_for` returns one mapping across both
tables."""

MAX_DESCRIPTION_CHARS = 600
"""Descriptions are restaurant-authored free text with no server-side length
bound. Embedding providers charge by token and truncate silently at their
own limit; truncating HERE means the text that was embedded is the text
that was stored, so `content_hash` keeps meaning what it says."""

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class RestaurantKnowledge:
    """Everything one restaurant contributes to the index: exactly one
    restaurant chunk and its dishes."""

    restaurant_id: str
    restaurant: RestaurantChunk
    items: Sequence[ItemChunk]


def _clean(text: str | None) -> str:
    """Collapse whitespace so a reformatted description — a newline added, a
    double space removed — is not mistaken for a changed one."""
    return _WHITESPACE.sub(" ", text).strip() if text else ""


def _mapping(value: object) -> Mapping[str, Any]:
    """Untrusted JSON, narrowed exactly once.

    Every traversal below goes through this and `_list`, so a payload shape
    we did not expect degrades to "nothing here" instead of raising inside a
    Kafka handler — where the cost of a TypeError is a parked message and a
    restaurant missing from search, not a stack trace someone reads.
    """
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _list(value: object) -> Sequence[Any]:
    return cast(Sequence[Any], value) if isinstance(value, list) else ()


def _slugs(values: object) -> list[str]:
    """Lowercase, de-duplicated, SORTED. Catalog already normalises tags and
    cuisines to lowercase slugs at its API layer; this repeats it because a
    read model that trusts an upstream invariant is a read model that breaks
    when the upstream adds one caller."""
    cleaned = {_clean(str(v)).lower() for v in _list(values)}
    return sorted(slug for slug in cleaned if slug)


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def fingerprint(payload: Mapping[str, Any]) -> str:
    """A stable identity for one staged payload.

    `sort_keys` because JSON object order is not semantic: the same menu
    re-serialised with its keys shuffled is the same menu, and treating it
    as new work would cost a re-drain every time an upstream library
    changed its dict ordering. The drain's guarded completion compares this,
    so the only thing that must be true is that DIFFERENT states differ.
    """
    return _hash(json.dumps(payload, sort_keys=True, default=str))


def _restaurant_text(name: str, branch_label: str | None, cuisines: Sequence[str]) -> str:
    """Identity, not location: `city` is a hard predicate on the row, so
    putting it in the prose would only add noise to the vector."""
    display = f"{name} — {branch_label}" if branch_label else name
    lines = [display]
    if cuisines:
        lines.append(f"Cuisines: {', '.join(cuisines)}")
    return "\n".join(lines)


def _item_text(
    name: str,
    description: str,
    category: str,
    tags: Sequence[str],
    cuisines: Sequence[str],
) -> str:
    """FR-58's field list exactly — name, description, tags, category,
    cuisine — and deliberately NOT the restaurant's name.

    Dropping the restaurant name is what makes the ADR-0028 fan-out
    affordable. A base item inherited by twelve branches arrives as twelve
    full-state events; with the branch name in the text, that is twelve
    distinct strings and twelve embedding calls for one sentence. Without
    it, the text is byte-identical, so one embedding serves all twelve rows
    once the drain dedupes by `content_hash`. The restaurant chunk carries
    the name, and retrieval fuses the two legs anyway.
    """
    lines = [name]
    if description:
        lines.append(description[:MAX_DESCRIPTION_CHARS])
    lines.append(f"Category: {category}")
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")
    if cuisines:
        lines.append(f"Cuisines: {', '.join(cuisines)}")
    return "\n".join(lines)


def is_indexable(payload: Mapping[str, Any]) -> bool:
    """Is this payload worth staging at all?

    The consumer asks this before writing a pending row so a brand's fan-out
    event does not queue work the drain would only throw away. `chunk()`
    asks it too, so there is exactly one definition of "indexable" and the
    consumer cannot drift from the thing that does the work.

    - **A brand** (`kind == "brand"`) is a menu TEMPLATE, not a place
      (ADR-0028) — no address, no schedule, nothing to pause, and catalog
      says so by leaving `city` and `status` null on brand rows. Every
      branch receives its own full effective-state event through the
      fan-out, so skipping the brand loses nothing and keeps template rows
      out of results BY CONSTRUCTION rather than by a filter someone must
      remember to write (FR-63).
    - **A branch with no city** is unplaceable, and every query is
      geo-scoped: indexing it would create a row no predicate can reach and
      no operator can explain.
    """
    return payload.get("kind") != "brand" and bool(_clean(payload.get("city")))


def chunk(*, restaurant_id: str, payload: Mapping[str, Any]) -> RestaurantKnowledge | None:
    """One catalog event -> everything that restaurant contributes.

    `restaurant_id` is passed separately because it is NOT in the payload:
    it is the envelope's `aggregate_id`, which is also the topic key, which
    is what makes per-restaurant ordering and compaction work. Analytics'
    repoint consumer reads it the same way.

    Returns None for anything `is_indexable` rejects — see there for why.
    """
    if not is_indexable(payload):
        return None

    city = _clean(payload.get("city"))
    cuisines = _slugs(payload.get("cuisines"))
    brand_id = payload.get("brand_id")
    # catalog's column is `open | paused`; an event that predates the column
    # is treated as open, because a restaurant that is serving orders and
    # missing from search is the worse failure.
    status = _clean(payload.get("status")) or "open"

    text = _restaurant_text(
        _clean(payload.get("name")), _clean(payload.get("branch_label")), cuisines
    )
    restaurant = RestaurantChunk(
        id=f"{restaurant_id}:{SELF_SUFFIX}",
        restaurant_id=restaurant_id,
        city=city,
        brand_id=brand_id,
        cuisines=cuisines,
        status=status,
        content=text,
        content_hash=_hash(text),
    )

    items: list[ItemChunk] = []
    for raw_category in _list(_mapping(payload.get("menu")).get("categories")):
        category = _mapping(raw_category)
        category_name = _clean(category.get("name"))
        for raw_item in _list(category.get("items")):
            item = _mapping(raw_item)
            item_id = item.get("id")
            if not item_id:
                continue
            tags = _slugs(item.get("tags"))
            name = _clean(item.get("name"))
            item_text = _item_text(
                name,
                _clean(item.get("description")),
                category_name,
                tags,
                cuisines,
            )
            items.append(
                ItemChunk(
                    id=f"{restaurant_id}:{item_id}",
                    restaurant_id=restaurant_id,
                    item_id=str(item_id),
                    city=city,
                    brand_id=brand_id,
                    cuisines=cuisines,
                    category=category_name,
                    tags=tags,
                    name=name,
                    # An 86'd dish stays INDEXED, flagged unavailable. It
                    # comes back tomorrow, and deleting it would mean paying
                    # to re-embed unchanged text every time a kitchen runs
                    # out of chicken.
                    price_cents=int(item.get("price_cents") or 0),
                    available=bool(item.get("available")),
                    status=status,
                    content=item_text,
                    content_hash=_hash(item_text),
                )
            )
    return RestaurantKnowledge(restaurant_id=restaurant_id, restaurant=restaurant, items=items)

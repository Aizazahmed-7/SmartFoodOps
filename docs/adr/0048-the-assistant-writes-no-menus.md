# 0048 — The assistant writes no menus

**Status**: Accepted (2026-10-05)

## Context

B6's content studio drafts menu descriptions and a human publishes them.
FR-93 is specific about the second half: "publish goes through the ordinary
Catalog write path and triggers re-embedding".

The obvious implementation is one call. An admin presses **Approve &
publish**; the assistant marks the draft published and PATCHes the dish. One
request, one transaction-shaped action, nothing for a client to get wrong.

It requires giving the assistant the ability to write a live menu, and
Catalog's item endpoint is `RestaurantAdmin` with an ownership check — so
it would need either a service identity impersonating the admin, or a new
SystemOnly write endpoint. The second is not the ordinary path by
definition. The first is impersonation.

Both are worse than they look, because of what the assistant is. ADR-0029
and ADR-0030 make the GenAI plane advisory and separately shed-able: it can
be switched off, rate-limited, or fail, and ordering continues. A plane
with menu-write authority is no longer that. It becomes a component whose
compromise — a prompt injection, a bug in a tenancy check, a confused
deputy — edits what customers see on real menus.

## Decision

1. **The assistant never writes to the catalog.** It has no catalog client,
   no menu-write credential, and no code path to one. A test scans
   `drafts.py`'s imports and fails if it grows one.

2. **The publish is the admin's own PATCH**, from the console, with their
   own token, against the same `/v1/restaurants/{id}/items/{item_id}`
   endpoint the Menu tab uses to edit a description by hand. "Ordinary" is
   meant literally.

3. **Approve records the decision.** Who approved it, when, and the text
   they actually shipped — kept alongside the text the model wrote, because
   the gap between them is the only measure of how much people have to fix.

4. **Catalog first, record second.** The console writes the menu and then
   tells the assistant. A failure between the two leaves a draft that still
   needs action rather than a row claiming a publication that did not
   happen; re-approving the same text is idempotent so the retry is not
   refused, and different text is a 409 because a second publication is a
   second decision.

## Consequences

**Re-embedding needed no code.** FR-93's "triggers re-embedding" is the
existing catalog-change pipeline doing what it already does — which is the
argument for the ordinary path stated as a result rather than a hope. Proven
live end to end: PATCH, then the chunk's embedded text carried the new
description after the debounce window.

**The ordering constraint is real and lives in a client.** That is the cost.
It is mitigated by the failure direction being the safe one, and it was
validated by accident: the console patched a branch id instead of the
brand's, Catalog returned "unknown item", and the draft correctly stayed
`drafted` with the admin still looking at work to do.

**A second publish surface would have to repeat this.** Promotions and
engagement copy are drafted, approved and recorded, and then a human takes
them elsewhere — there is no promotions surface in Part A to publish into.
When one exists, it gets the same handshake rather than a shortcut, for the
same reason.

**The GenAI plane stays sheddable.** Turning the assistant off costs
drafting. It cannot cost a menu, because the assistant was never the thing
that wrote one.

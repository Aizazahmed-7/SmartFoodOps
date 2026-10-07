# 0034 — Multi-role identity, role-specific tables, and one session row per login

**Status**: Accepted (2026-09-10) — amends [ADR-0022](0022-roles-as-seeded-lookup-table.md)
(single-role assumption) and [ADR-0005](0005-jwt-verified-once-at-edge.md) (the `role`
claim and the `X-Auth-Role` header)

## Context

`users.role` held exactly one role, and promotion **overwrote** it:
`grant_restaurant_admin` set `role = 'restaurant_admin'`, so a customer who
opened a restaurant stopped being a customer in the data model while remaining
one in every other sense. Authorization then had to patch the lost fact back in
by naming both roles at every customer-facing gate:

```python
Purchaser = Annotated[AuthContext, Depends(require_role(Role.CUSTOMER, Role.RESTAURANT_ADMIN))]
```

CLAUDE.md recorded the cost of that in its own invariants list: *"promoted owners
order dinner too. **This bug has recurred at every new customer-facing
endpoint**; check it first."* A model that loses a fact, plus a convention that
every future endpoint must remember to restore it, is a defect generator — four
gates carried the workaround and every new one was another chance to forget.

Two adjacent findings from the same review:

- **`users.rider_id` was always set to `users.id`** (`grant_rider` wrote
  `{"role": "rider", "rider_id": user_id}`) — a primary-key duplicate carrying
  no information, while ADR-0022 had already named the rider table it should
  become "when riders grow rider-only state (vehicle, documents)".
- **`refresh_tokens` grew one row per token**, grouped by `family_id`. With a
  15-minute access token, an active 30-day session wrote ~480 rows, and
  **nothing prunes them** — the table had no ceiling at all.

## Decision

1. **Roles become a set.** `user_roles(user_id, role, granted_at)`, FK'd to the
   existing seeded `roles` table — which is what finally makes ADR-0022's
   lookup a real many-to-many target rather than a lookup for a single-valued
   column. Its enum-authority contract and pin test are unchanged. Migration
   0005 backfills every user's current role **and restores `customer` for every
   promoted user**, which is provable: both grant paths required
   `role == 'customer'`, so every owner and rider demonstrably was one.
   `system`/`system_admin` are not people and get no customer role.

2. **The wire carries the set.** The JWT claim is `roles: [str]`; the stamped
   header is `X-Auth-Roles` (sorted, comma-joined). `AuthContext.roles` is a
   `frozenset[str]` with `min_length=1` — a context with no role can pass no
   gate, so it is not an identity. `require_role` is a set intersection.
   **`X-Auth-Roles` is in `STRIP_HEADERS`**: it is trusted downstream, so a
   client copy would be self-service privilege escalation. `X-Auth-Role` stays
   listed there after being retired, because an un-upgraded service mid-deploy
   still trusts it.

3. **Role-specific state gets its own tables.** `riders(user_id, onboarded_at)`
   is the home ADR-0022 deferred; `restaurant_owners(user_id, brand_id,
   granted_at)` holds the grant, with `user_id` as PK expressing
   one-brand-per-owner in identity, mirroring catalog's partial unique index.
   `users` is now identity only: id, email, password_hash, full_name, phone.
   Operational rider state (status, `active_deliveries`, `offer_lock`) stays
   dispatch's DynamoDB truth (ADR-0026) and is **never** mirrored into `riders`.

4. **`GrantConflict` survives as an explicit rule.** "Riders can't own
   restaurants" was previously imposed for free by there being one role column.
   It is now written out — whether riders *should* be able to own a restaurant
   is a product question this refactor deliberately did not answer.

5. **One `refresh_tokens` row per LOGIN, rotated in place.** `family_id` is gone
   because the row *is* the session; `revoked` is gone with the reuse-detection
   branch that was its only reader. **Token-theft detection is given up by
   decision**: overwriting `token_sha256` makes a replayed stolen token
   indistinguishable from garbage. Rotation's remaining value is that a stolen
   token stops working at the legitimate holder's next refresh. Multi-device
   sessions are retained (product decision), which is why one row per *session*
   rather than one per *user*.

6. **Rollout was additive in four steps**, each leaving `make cov` at 100% and
   `make lint` clean: identity-local (emitting both claim forms) → the auth lib
   (`roles` alongside a derived `role`) → the three consumers → deletion. The
   frontend was migrated **before** the server stopped emitting `role`, because
   it decodes the claim client-side and deploys independently.

## Consequences

**Positive**

- The purchaser-gate bug class is **removed rather than mitigated**: a promoted
  owner genuinely holds `customer`, so no future endpoint has to remember to
  name both roles. CLAUDE.md's invariant about it is retired.
- `users` has no role-shaped nullable columns; `rider_id`'s duplicated
  primary key is gone.
- Refresh-token growth drops from per-refresh to per-login — roughly 480× — and
  `user_id` finally has an index (only `family_id` had one before).
- Revocation of one session is a single-row write instead of a family scan.

**Negative**

- **No token-theft detection.** The append-per-token model caught a replayed
  ancestor and revoked the whole lineage; nothing does now. Mitigated only by
  the 15-minute access TTL and rotation overwriting the stolen value.
- A role *set* means a token's blast radius is the union of its roles.
- The `riders`/`restaurant_owners` rows cannot be declared to match the role
  they accompany — nothing enforces "a `riders` row exists iff the `rider` role
  is held". Both are written in the same transaction as their grant, which is
  what keeps them in step.
- Migration 0005 **truncates `refresh_tokens`** (a family cannot be mapped to
  one row faithfully) and 0006's downgrade is lossy by definition: a promoted
  owner's `customer` role cannot be represented in one column.

**Owed, not done**

- **No reaper on `expires_at` and no logout endpoint.** Growth is bounded per
  login but still has no ceiling, and a user cannot end a session deliberately.
  When logout lands it should `DELETE` the row rather than flag it — that also
  covers force-signout as `DELETE WHERE user_id`.
- Notification's `_recipient()` keeps **owner-wins**: a user holding both roles
  reads the restaurant bell, not both. Serving both means returning a list,
  authorizing several channels on the ticket, and subscribing the SSE stream to
  more than one — a notification redesign, not identity work.

**Revisit trigger**: a permissions model (role → permission as data) — at which
point ADR-0022's enum-authority contract is renegotiated as that design's
subject, and `user_roles` becomes the join it was always shaped like.

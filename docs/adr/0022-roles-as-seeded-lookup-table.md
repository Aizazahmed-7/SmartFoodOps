# 0022 — Roles as a seeded lookup table, pinned to the enum

**Status**: Accepted 2026-08-14 (team review decision)

## Context

Team review requested a `roles` table with a foreign key from `users`, aligning with the org's reference architecture (web-controller). Our study of that system found its role table written only by migrations, with the code enum still authoritative — and four live drift defects (role names mapped that exist in no row, a consumer keying on a field no producer emits, checkers never wired, duplicated checks reading different fields). The risk of a role table is not the table; it is **unpinned dual sources of truth**.

## Decision

Adopt the table, engineered so the drift class cannot exist:

- **`roles(name TEXT PK, created_at)`** — keyed by the role *name*, no synthetic id. `users.role` stays the plain string (wire format, JWT claims, `X-Auth-*` headers, and every `require_role` gate untouched; no join anywhere). The FK replaces the old CHECK as the DB-side guard.
- **The `smartfood_auth.Role` enum remains the single authority.** The table is *derived*: `seed_roles()` converges it to the enum idempotently **on every service boot** (both create_all and migrated paths), so a new enum member can never be forgotten in a deploy. Migration 0004 seeds before the FK lands.
- **A pin test makes divergence a red build**: table contents must equal the enum exactly, both directions.
- Adding a role = add the enum member (+ the feature code that gives it meaning). The row appears on next boot. Removing a role = a real migration with a user-data plan (FK references exist).

## Consequences

**Positive**
- Referential integrity for `users.role`; the table exists for future metadata (display names, a permissions model) without schema churn at that point.
- Org-consistent design, minus the reference system's failure modes: no second vocabulary, no unseeded names, no join, no id mapping.

**Negative**
- A second artifact to keep pinned (mitigated to zero marginal effort by boot-time seeding + the pin test, but the machinery exists).
- CHECK-style rejection of an unknown role now surfaces as an FK violation instead — equivalent guarantee, different error text.

**Revisit trigger**: a runtime-configurable permissions feature (role → permission mapping as data) — at that point the table becomes genuinely load-bearing and the enum-authority contract is renegotiated as part of that design.

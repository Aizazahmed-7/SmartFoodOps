# SmartFoodOps — ERD (as built, W3)

**Read this first:** the platform is database-per-service — six PostgreSQL databases, one per owning service, and **no foreign keys ever cross a database**. Real FK lines appear only inside each diagram; references _between_ services travel as plain id columns (shown in the last diagram) and are kept consistent by events + idempotent consumers, not constraints. Two table shapes come from shared libs: `outbox` (smartfood-outbox, 9 columns) repeats across services; `idempotency_keys` (smartfood-idempotency) survives only in payment_db — order's copy was retired by ADR-0024 (the orders row itself is placement's idempotency record).

---

## identity_db — who you are

```mermaid
erDiagram
    roles {
        text name PK "seeded FROM the Role enum every boot; pin-tested (ADR-0022)"
        timestamptz created_at
    }
    users {
        text id PK
        text email UK
        text password_hash
        text full_name
        text phone
        text role FK "-> roles.name; the enum stays authoritative"
        text restaurant_id "set by grant; scopes partner access"
        text rider_id
        timestamptz created_at
    }
    addresses {
        text id PK
        text user_id FK
        text label
        text line1
        text city
        float lat
        float lon
        timestamptz created_at
    }
    refresh_tokens {
        text id PK
        text family_id "kill the whole family on reuse"
        text user_id FK
        text token_sha256 UK
        timestamptz expires_at
        timestamptz rotated_at "non-null = already rotated"
        boolean revoked
        timestamptz created_at
    }
    processed_events {
        text consumer_group PK
        text event_id PK
        timestamptz processed_at
    }
    roles ||--o{ users : "referenced by name"
    users ||--o{ addresses : "has"
    users ||--o{ refresh_tokens : "session families"
```

### Example rows — the tricky identity tables

`roles` — the seeded lookup (ADR-0022): five rows, one per `Role` enum member, written only by boot-time seeding — a test pins table == enum, both directions:

| name             | created_at |
| ---------------- | ---------- |
| customer         | 12:00      |
| restaurant_admin | 12:00      |
| rider            | 12:00      |
| system_admin     | 12:00      |
| system           | 12:00      |

`refresh_tokens` — one login's **family**, rotated twice. Reuse of `rt_1` or `rt_2` now = theft signal → the whole `fam_a` chain is revoked:

| id   | family_id | user_id | rotated_at                   | revoked |
| ---- | --------- | ------- | ---------------------------- | ------- |
| rt_1 | fam_a     | usr_1   | 12:00 _(exchanged for rt_2)_ | false   |
| rt_2 | fam_a     | usr_1   | 14:30 _(exchanged for rt_3)_ | false   |
| rt_3 | fam_a     | usr_1   | NULL _(the live one)_        | false   |

`processed_events` — the grant-convergence consumer's memory. Note the second row: a **failure verdict** ("can never apply") is also recorded, so redeliveries stop re-alarming:

| consumer_group             | event_id                                                     | processed_at |
| -------------------------- | ------------------------------------------------------------ | ------------ |
| identity.grant-convergence | `a3f1…` _(RestaurantCreated rst_9 — grant applied)_          | 12:01        |
| identity.grant-convergence | `77b0…` _(owner was a rider — GrantConflict, marked anyway)_ | 12:05        |

## catalog_db — what can be ordered

```mermaid
erDiagram
    restaurants {
        text id PK
        text owner_user_id UK "logical -> identity.users.id"
        text name
        text city
        float lat
        float lon
        text status "CHECK: open|paused"
        json hours
        int version "bumps on EVERY mutation"
        timestamptz created_at
        timestamptz updated_at
    }
    restaurant_cuisines {
        text restaurant_id PK "FK"
        text cuisine PK
    }
    menu_categories {
        text id PK
        text restaurant_id FK
        text name
        int rank
    }
    menu_items {
        text id PK
        text restaurant_id FK
        text category_id FK
        text name
        text description "feeds FTS search"
        int price_cents "integer cents, never floats"
        text currency
        boolean available "the 86 flag"
        int rank
    }
    item_tags {
        text item_id PK "FK"
        text tag PK
    }
    modifier_groups {
        text id PK
        text item_id FK
        text name
        int min_select
        int max_select
        int rank
    }
    modifier_options {
        text id PK
        text group_id FK
        text name
        int price_delta_cents
        int rank
    }
    menu_versions {
        text restaurant_id PK "FK"
        int version PK
        timestamptz published_at
    }
    outbox {
        text id PK "UUIDv5 of the fact"
        text aggregate_type
        text aggregate_id
        int aggregate_version
        text event_type
        json payload "FULL STATE incl. owner + menu"
        timestamptz occurred_at
        timestamptz published_at "NULL = undrained"
        text traceparent
    }
    restaurants ||--o{ restaurant_cuisines : ""
    restaurants ||--o{ menu_categories : ""
    restaurants ||--o{ menu_items : ""
    menu_categories ||--o{ menu_items : ""
    menu_items ||--o{ item_tags : ""
    menu_items ||--o{ modifier_groups : ""
    modifier_groups ||--o{ modifier_options : ""
    restaurants ||--o{ menu_versions : "audit per mutation"
```

Search indexes worth knowing (Postgres-only — they live in migration 0001, not `db.py`, so sqlite's `create_all` never sees them): `pg_trgm` GIN indexes on `restaurants.name` and `menu_items.name` (typo-tolerant fuzzy match) plus FTS expression indexes over names + item descriptions — these four indexes ARE the search engine (ADR-0019).

### Example rows — versions and the outbox, concretely

One restaurant, three mutations. `restaurants.version` is the **current** counter; `menu_versions` is the **history**; the outbox holds one event **per mutation**, each with a computed id:

`restaurants` (current state only):

| id    | owner_user_id | name          | status | version |
| ----- | ------------- | ------------- | ------ | ------- |
| rst_9 | usr_1         | Biryani House | open   | **3**   |

`menu_versions` (when each version was born — gapless):

| restaurant_id | version | published_at            |
| ------------- | ------- | ----------------------- |
| rst_9         | 1       | 12:00 _(created)_       |
| rst_9         | 2       | 12:10 _(item added)_    |
| rst_9         | 3       | 12:15 _(price changed)_ |

`outbox` — read `id` together with the three inputs that computed it. `aggregate_type` + `aggregate_id` say _whose fact_, `aggregate_version` says _which occurrence_, `event_type` says _what kind_:

| id = uuid5 of…                                   | aggregate_type | aggregate_id | aggregate_version | event_type        | payload (full state!)                                       | published_at                                   |
| ------------------------------------------------ | -------------- | ------------ | ----------------- | ----------------- | ----------------------------------------------------------- | ---------------------------------------------- |
| `a3f1…` = "restaurant:rst_9:1:RestaurantCreated" | restaurant     | rst_9        | 1                 | RestaurantCreated | {owner_user_id: usr_1, name: …, menu: {…}}                  | 12:00                                          |
| `5c88…` = "restaurant:rst_9:2:ItemAdded"         | restaurant     | rst_9        | 2                 | ItemAdded         | {owner_user_id: usr_1, …, menu: {full menu incl. new item}} | 12:10                                          |
| `d901…` = "restaurant:rst_9:3:ItemUpdated"       | restaurant     | rst_9        | 3                 | ItemUpdated       | {owner_user_id: usr_1, …, menu: {full menu, new price}}     | **NULL** _(staged, poller hasn't drained yet)_ |

Why the versions matter: two `ItemUpdated` events on the same restaurant get **different** ids (versions 3 vs 4) — but a _redelivery_ of version 3 recomputes the **same** `d901…`, which is what lets every consumer dedupe.

## inventory_db — what can actually be sold

```mermaid
erDiagram
    stock {
        text item_id PK "logical -> catalog.menu_items.id"
        text restaurant_id "logical -> catalog.restaurants.id"
        int available "CHECK >= 0 — the oversell guard"
        int version
        timestamptz updated_at
    }
    restaurant_load {
        text restaurant_id PK
        int active "concurrent-order slots in use"
        int capacity
        int version
    }
    reservations {
        text order_id PK "logical -> order.orders; PK = replay idempotency"
        text restaurant_id
        json lines "the restore recipe for release"
        text status "CHECK: active|released|consumed|expired"
        int version
        timestamptz created_at
        timestamptz expires_at "reaper TTL"
    }
    outbox {
        text id PK
        text aggregate_type "stock | reservation"
        text aggregate_id
        int aggregate_version
        text event_type
        json payload
        timestamptz occurred_at
        timestamptz published_at
        text traceparent
    }
```

Index worth knowing: `ix_reservations_reaper (status, expires_at)` — the reaper's scan for overdue actives.

### Example rows — a reservation's life against the stock it holds

`stock` — before `ord_42` reserves 2 portions, and after (note `version` bumps on every mutation; `StockAdjusted` events computed from it):

| item_id                       | restaurant_id | available | version |
| ----------------------------- | ------------- | --------- | ------- |
| itm_biryani _(before)_        | rst_9         | 100       | 4       |
| itm_biryani _(after reserve)_ | rst_9         | **98**    | 5       |

`reservations` — three orders, three fates. `lines` is the restore-recipe; `expires_at` is the reaper's clock:

| order_id | status       | lines                            | expires_at | what happened                                          |
| -------- | ------------ | -------------------------------- | ---------- | ------------------------------------------------------ |
| ord_42   | **consumed** | [{item_id: itm_biryani, qty: 2}] | 12:52      | settled — stock stays sold, slot freed                 |
| ord_43   | **released** | [{item_id: itm_biryani, qty: 1}] | 12:55      | card declined — stock restored                         |
| ord_44   | **expired**  | [{item_id: itm_raita, qty: 3}]   | 12:58      | saga died holding it — the reaper released it at 12:58 |

`restaurant_load` — slots as stock; two orders currently cooking:

| restaurant_id | active | capacity | version |
| ------------- | ------ | -------- | ------- |
| rst_9         | 2      | 10       | 1       |

## order_db — the state machine

```mermaid
erDiagram
    orders {
        text order_id PK
        text user_id "logical -> identity.users.id"
        text restaurant_id "logical -> catalog.restaurants.id"
        text restaurant_name_snapshot
        text status "CHECK: 13 states PLACED..SETTLED"
        int aggregate_version
        text payment_method "CHECK: CARD|COD"
        text card_token
        text request_hash "ADR-0024: body guard; NULL = pre-0024 row"
        int menu_version "pinned at placement"
        json pricing_snapshot "totals; activities READ, never recompute"
        json delivery_address_snapshot
        text cancel_reason
        timestamptz placed_at
        timestamptz updated_at
    }
    order_items {
        text order_id PK "FK"
        int line_no PK
        text menu_item_id "logical -> catalog.menu_items.id"
        text name_snapshot
        int unit_price_cents
        int qty "CHECK 1..50"
        json options_snapshot
        int line_total_cents
    }
    outbox {
        text id PK
        text aggregate_type "order"
        text aggregate_id
        int aggregate_version
        text event_type
        json payload "full state per event"
        timestamptz occurred_at
        timestamptz published_at
        text traceparent
    }
    orders ||--o{ order_items : "line snapshots"
```

Indexes worth knowing: `ix_orders_history (user_id, placed_at DESC, order_id DESC)` — customer keyset paging; `ix_orders_feed (restaurant_id, status, placed_at)` — kitchen queues.

### Example rows — one order, its versions, and its gappy event trail

`orders` — the finished `ord_42`. `aggregate_version=9` means nine guarded transitions happened; `menu_version=7` pins _which_ menu priced it:

| order_id | user_id | restaurant_id | status  | aggregate_version | menu_version | pricing_snapshot                                        |
| -------- | ------- | ------------- | ------- | ----------------- | ------------ | ------------------------------------------------------- |
| ord_42   | usr_1   | rst_9         | SETTLED | **9**             | 7            | {subtotal: 3000, fee: 199, tax: 247, total_cents: 3446} |

`order_items` — the cart snapshot (survives any future menu edit):

| order_id | line_no | menu_item_id | name_snapshot   | unit_price_cents | qty | options_snapshot                                           | line_total_cents |
| -------- | ------- | ------------ | --------------- | ---------------- | --- | ---------------------------------------------------------- | ---------------- |
| ord_42   | 1       | itm_biryani  | Chicken Biryani | 1200             | 2   | [{group_name: Size, name: Family, price_delta_cents: 600}] | 3600             |

`outbox` — the **gappy** version sequence made visible. Nine transitions, but only four staged events (the in-between statuses bump silently):

| aggregate_version | event_type     | id = uuid5 of…                                                                      |
| ----------------- | -------------- | ----------------------------------------------------------------------------------- |
| 0                 | OrderPlaced    | "order:ord_42:0:OrderPlaced"                                                        |
| 3                 | OrderConfirmed | "order:ord_42:3:OrderConfirmed" _(v1 VALIDATED, v2 PAYMENT_CLEARED staged nothing)_ |
| 8                 | OrderDelivered | "order:ord_42:8:OrderDelivered" _(v4–v7 = kitchen + pickup, silent)_                |
| 9                 | OrderSettled   | "order:ord_42:9:OrderSettled"                                                       |

**Idempotency without a table (ADR-0024)** — `order_id = ord_ + uuid5(NS, "usr_1:K-7f3a…")`, so the ROW is the record:

| retry with key K-7f3a… finds | answer |
| ---------------------------- | ------ |
| row, `request_hash` matches | 202 `{order_id: ord_42, status: <current>}` + `Idempotent-Replay: true` |
| row, hash differs | 422 `IDEMPOTENCY_KEY_REUSE` — same key, different cart |
| no row, workflow running | attaches via `USE_EXISTING` / the `await_placement` probe |
| no row, nothing running | places fresh — same derived id either way |

## payment_db — the money

```mermaid
erDiagram
    payments {
        text order_id PK "one payment per order, keyed by it"
        text status "CHECK: AUTHORIZED|DECLINED|CAPTURED|VOIDED|REFUNDED"
        int amount_cents "CHECK >= 1; from stored auth, never the caller"
        text currency
        text card_token
        text psp
        text payment_intent_id "the PSP's ref"
        timestamptz capture_before
        int version
        timestamptz created_at
        timestamptz updated_at
    }
    ledger {
        text entry_id PK
        text order_id "indexed"
        text op_key "{order_id}:capture etc."
        text account "customer | platform_cash"
        int debit_cents "CHECK: exactly one side > 0"
        int credit_cents
        text currency
        timestamptz created_at
    }
    idempotency_keys {
        text scope PK "money"
        text idem_key PK "{order_id}:auth|capture|void|refund"
        text body_hash
        text status
        int response_status
        json response_body
        timestamptz created_at
    }
    webhook_events {
        text webhook_id PK
        text psp
        json payload
        timestamptz received_at
    }
    outbox {
        text id PK
        text aggregate_type "payment"
        text aggregate_id "the order_id"
        int aggregate_version
        text event_type
        json payload
        timestamptz occurred_at
        timestamptz published_at
        text traceparent
    }
    payments ||--o{ ledger : "append-only pairs"
```

`ledger` is append-only (a source-scan test bans UPDATE/DELETE): corrections are reversing pairs, never edits. `webhook_events` is built but unwritten until a real PSP replaces the mock.

### Example rows — money keys and balanced pairs

`idempotency_keys` (scope "money") — one row per money verb, the natural key IS `{order_id}:{op}`:

| scope | idem_key       | status   | response_status                                       |
| ----- | -------------- | -------- | ----------------------------------------------------- |
| money | ord_42:auth    | COMPLETE | 200                                                   |
| money | ord_42:capture | COMPLETE | 200                                                   |
| money | ord_43:auth    | COMPLETE | 402 _(a DECLINE is also a stored, replayable answer)_ |

`ledger` — append-only pairs. `ord_42` was captured; `ord_45` was captured then refunded — note the refund is a **reversing pair**, never an edit:

| entry_id | order_id | op_key         | account       | debit_cents | credit_cents |
| -------- | -------- | -------------- | ------------- | ----------- | ------------ |
| led_01   | ord_42   | ord_42:capture | customer      | 3446        | 0            |
| led_02   | ord_42   | ord_42:capture | platform_cash | 0           | 3446         |
| led_03   | ord_45   | ord_45:capture | customer      | 2100        | 0            |
| led_04   | ord_45   | ord_45:capture | platform_cash | 0           | 2100         |
| led_05   | ord_45   | ord_45:refund  | platform_cash | 2100        | 0            |
| led_06   | ord_45   | ord_45:refund  | customer      | 0           | 2100         |

Every row has exactly one non-zero side (the CHECK); every op sums to zero across accounts — the books always balance.

## notification_db — the inbox (consumer-only: no outbox)

```mermaid
erDiagram
    notifications {
        text id PK "ntf_uuid5(event_id + recipient) — dedupe IS the key"
        text recipient_type "CHECK: customer|restaurant"
        text recipient_id "user_id or restaurant_id"
        text order_id "logical -> order.orders"
        text kind
        text title
        text body
        timestamptz created_at "event occurred_at, replay-stable"
        timestamptz read_at "NULL = unread"
    }
    order_recipients {
        text order_id PK
        text user_id "payment events carry no user_id;"
        text restaurant_id "this projection is the join"
    }
```

Index: `ix_notifications_inbox (recipient_type, recipient_id, created_at DESC, id DESC)` — one keyset walk per bell poll.

---

### Example rows — one event fanning out to two inboxes

`order_recipients` — the projection that lets user-less payment events find their customer:

| order_id | user_id | restaurant_id |
| -------- | ------- | ------------- |
| ord_42   | usr_1   | rst_9         |

`notifications` — the single `OrderConfirmed` event (id `b7e4…`) minted **two** rows, each with its own deterministic id, so each recipient's copy dedupes independently on redelivery:

| id = ntf_uuid5 of…       | recipient_type | recipient_id | kind            | title               | read_at                          |
| ------------------------ | -------------- | ------------ | --------------- | ------------------- | -------------------------------- |
| "b7e4…:restaurant:rst_9" | restaurant     | rst_9        | order_confirmed | New order to accept | 12:03                            |
| "b7e4…:customer:usr_1"   | customer       | usr_1        | order_confirmed | Order confirmed     | NULL _(unread — the bell badge)_ |

---

## Cross-service references — ids, not FKs

These lines are _conventions kept true by events and idempotent consumers_, never constraints the databases enforce:

```mermaid
flowchart LR
    subgraph identity_db
        users[users]
        addresses[addresses]
    end
    subgraph catalog_db
        restaurants[restaurants]
        menu_items[menu_items]
    end
    subgraph order_db
        orders[orders]
        order_items[order_items]
    end
    subgraph inventory_db
        stock[stock]
        reservations[reservations]
    end
    subgraph payment_db
        payments[payments]
    end
    subgraph notification_db
        notifications[notifications]
        order_recipients[order_recipients]
    end

    restaurants -. "owner_user_id" .-> users
    users -. "restaurant_id (grant)" .-> restaurants
    orders -. "user_id" .-> users
    orders -. "restaurant_id" .-> restaurants
    orders -. "delivery_address_snapshot (copied)" .-> addresses
    order_items -. "menu_item_id (snapshot)" .-> menu_items
    stock -. "item_id" .-> menu_items
    reservations -. "order_id" .-> orders
    payments -. "order_id" .-> orders
    notifications -. "order_id" .-> orders
    order_recipients -. "user_id" .-> users
    order_recipients -. "restaurant_id" .-> restaurants
```

Two idioms to notice while reading:

- **Snapshots over joins.** `orders` copies the restaurant _name_, the _address_, the _prices_ at placement time. The order is a historical fact; a later menu edit or address change must not rewrite what you bought. Where a normalized design would join, this design copies-at-commit.
- **The same four service-local columns everywhere.** `version` (optimistic bump feeding deterministic event ids), `status` with a CHECK against a closed vocabulary, an `outbox` staged in-transaction, and either `processed_events` or a natural key for consumer dedupe. Learn them once, and every service's schema reads the same.

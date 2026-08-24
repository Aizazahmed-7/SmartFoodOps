# Panel demo — Postman collection

Import `SmartFoodOps.postman_collection.json` (Postman → Import → File). One
collection, 69 requests in 9 folders, run **top to bottom, one Send at a
time**. Every request carries PASS/FAIL tests (watch the **Test Results** tab
go green) and captures what the next request needs into collection variables —
nothing is ever pasted by hand.

## Before the panel

```bash
make up-m3 && make seed        # m2 stack + notification service, seeded demo data
```

Gateway `http://localhost:8080` (the collection's `baseUrl`), Temporal UI
`http://localhost:8233` — keep it open in a tab; folder 03 pairs well with it.

**Open the Postman Console** (⌥⌘C / View → Show Postman Console): folder 03's
pre-request scripts print the **predicted order id** — computed locally with
`uuid5(sub:key)` — *before* the server sees the request. The test then holds
the server to it. That one green check is ADR-0023/0024 in a single frame.

## The story, folder by folder

| Folder | What the panel sees |
|---|---|
| **00 · Cast & stage** | tokens + ids captured; the JWT's `sub` decoded (it becomes half of every derived order id) |
| **01 · Auth** | rotation, then **token-theft detection**: replaying a rotated refresh kills the whole family — attacker *and* victim |
| **02 · Quote** | server-only pricing; every refusal coded (404 / ITEM_UNAVAILABLE / 422 / RESTAURANT_CLOSED via live pause) — ends by **re-pinning menuVersion**, because pause/resume bumped it (that's version-consent working) |
| **03 · Placement** | the star: predicted id === server id, replay via the orders row with **current** status, `request_hash` 422, PRICE_CHANGED, ownership-404 |
| **04 · Kitchen** | accept 202 → replay 200/202 → **opposite verdict 409** → preparing → ready → track to SETTLED (capture only after delivery) |
| **05 · Cancel & decline** | cancel 202/200/409-too-late; `tok_decline` still 202s — the decline arrives as order state (`payment_declined`), never a 500 |
| **06 · Notifications** | the bell narrates everything the panel just did; mark-read; owner's restaurant view |
| **07 · Chaos (optional)** | each description names the docker command: Temporal down → **503 with nothing written**, same key retries onto the predicted id; worker down → **202 pending**, 404 read window, then the order materializes |
| **08 · Onboarding** | a stranger becomes a restaurant: create → **403 on the stale token** → refresh → `restaurant_admin` → menu → **first order CANCELLED `item_unavailable`** (strict stock, the row inventory created at 0 over Kafka) → stock it → CONFIRMED, 50 → 48 |

## Chaos commands (folder 07 descriptions repeat these)

```bash
cd "/Users/emumba/Smart Food/SmartFoodOps"
docker compose -f deploy/compose/docker-compose.yml --profile core --profile apps stop temporal
docker compose -f deploy/compose/docker-compose.yml --profile core --profile apps start temporal
docker compose -f deploy/compose/docker-compose.yml --profile core --profile apps stop order-worker
docker compose -f deploy/compose/docker-compose.yml --profile core --profile apps start order-worker
```

## Timing notes

- Folder 04 must start within ~3 min of order A reaching CONFIRMED (the 180s
  accept window). If it lapses: the order self-cancels `restaurant_timeout` —
  itself demoable — then re-run folder 03's "Place order A" with a fresh Send.
- "Track" requests are deliberate poll points: press Send until the named
  status appears (saga ~2s; courier pickup ~20s; SETTLED ~50s after ready).
- The whole run is repeatable: every placement mints a fresh key, the panelist
  user is minted per run, and pause/resume cleans up after itself.
- Folder 08 depends on folder 00 only (it reuses `customerToken` + `addressId`
  for the buying half) and is otherwise self-contained — it mints its own owner,
  restaurant, category and item on every run, so it never collides with the seed.
- Folder 08's two "Read it" requests are poll points like folder 04's: the saga
  needs a moment. Re-send if the status has not landed yet.

Every expected status code in this collection was verified against the live
stack before it was committed (see the ADR-0024 drills) — the tests assert
observed behavior, not intentions.

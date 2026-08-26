# Dispatch milestone — overnight build walkthrough (2026-08-26)

> Plan → plan review → chunked implementation, per the delegation. Written
> as the work lands, for the morning teaching session. Base commit: 0c09572.

## The plan (C0)

**Goal:** replace the timer-courier with real dispatch — riders as
identities, the DDB-locked offer cascade (ADR-0011), GPS over WebSocket
(ADR-0006), the drivable game-map demo, and the analytics gap closing —
while every existing guarantee (compensations, cancel referee, capture
after delivery) survives untouched.

### Decisions inherited (not re-argued)

- ADR-0011: the DDB conditional write on `rider_state` is the ONLY lock.
  No Redis locks, no leases, anywhere. Redis GEO is a candidate index.
- ADR-0006: riders = WebSocket (JWT in `Sec-WebSocket-Protocol`),
  customers keep the S4/S9 SSE. ADR-0007: uniform-cardinality DDB keys —
  `rider_state` (PK rider_id), `deliveries` (PK order_id, GSI rider_id).
- FR-27/28/29/30/32 numbers: 1 Hz GPS, 30s latest-loc / 90s heartbeat
  TTLs, 3 km search (+widen to 6 after 3 misses), 15s/12s/12s cascade,
  READY-unassigned deadline → cancel through the EXISTING compensation
  path, never auto-cancel after pickup.
- User decisions (tonight's brief): the rider UI is OUR OWN 2D game map
  (SVG, arrow keys/click — no map tiles); frames are JSON (protobuf is a
  named seam); the load test stays parked.

### Decisions made tonight (each argued in its chunk)

1. **Toy world, real math.** Springfield gets a real lat/lon bounding box;
   seeded restaurants + the demo address get fixed coords (the columns
   already exist, nullable — the `hours` story again). GEOSEARCH, radii,
   haversine all run on genuine coordinates against a drawn city.
2. **Temporal owns the cascade clock.** The offer loop lives in
   DeliveryWorkflow (timers + signals are its native material): activity
   `find_and_offer` → 15s `wait_condition` on the accepted flag → miss →
   `revoke_offer` activity → next candidate. Liveness is workflow timers
   too: ASSIGNED without pickup by the deadline → conditional
   `revoke_assignment` → back to the cascade. No scanner daemon tonight
   (the 60s SCAN reconciliation is a named prod deferral).
3. **Dispatch is a service; the workflow talks to it through a port.**
   `services/dispatch` (:8012) owns DDB + GEO + scoring + offer
   bookkeeping. order-worker activities call it over internal HTTP
   (outcomes as values — the InventoryOpsPort pattern). Rider taps come
   the OTHER way: dispatch converts the lock, then POSTs order's internal
   courier-events endpoint, which signals the child — signal surface
   stays owned by order, the kitchen-decision precedent.
4. **The accept/expire race is resolved by DDB, read by the workflow.**
   Accept converts lock→assignment conditionally; the expiry revoke is
   conditional on the SAME offer_id still being locked; when revoke finds
   ASSIGNED instead, it answers `already_assigned` and the workflow
   proceeds as accepted — so even a LOST accept signal self-heals on the
   next timer. Every step one guarded write; no step a read-then-write.
5. **Offers are payloads, positions are polled.** The offer frame (WS)
   carries the work item — directed command, not broadcast truth, so the
   hints-only rule doesn't apply. The customer's courier dot POLLS a
   small authed endpoint (2s) reading Redis latest-loc — zero changes to
   the SSE lib; per-message event names on streams = named upgrade.
6. **REST is the floor, WS is the accelerator.** `GET /v1/rider/me`
   exposes the current offer too — so rider-sim, the e2e, and a rider
   with a dead socket all work on plain REST — the poll-floor philosophy
   applied to couriers. The gateway adds push latency, not correctness.
7. **dispatch.events publishes DIRECT to Kafka** (send_nowait, no outbox):
   dispatch's truth lives in DDB, so there is no PG transaction to be
   atomic with — the browse-events precedent. The prod answer is DDB
   Streams → Kafka (ADR-0026 records both).
8. **No dual courier mode.** The timer-courier dies tonight; demo/e2e
   scripts drive rider-sim instead. Two courier codepaths in one workflow
   = two determinism surfaces forever; rejected.
9. **New CancelReason `no_rider_available`** with customer + kitchen copy
   (the kitchen cooked; it deserves to know why the food has no ride).

### The chunks

| # | Chunk | Lands with |
|---|---|---|
| C1 | Rider identity + the toy city (grant role, rider_id claim, seeded riders/coords) | identity+seed tests |
| C2 | dispatch skeleton + DDB adapter + THE LOCK DRILL (N concurrent reserves → 1 winner, on moto) | moto suite |
| C3 | GEO index + scoring + offer service + internal/rider APIs + edge rules + compose | service suite |
| C4 | Workflow surgery: cascade + signal waits + pickup timeout + no-rider cancel + orders.rider_id | time-skipping suite |
| C5 | rider-gateway: WS auth, pings→GEO/heartbeat, 0.2 Hz→Kafka, offer relay + nginx | WS TestClient suite |
| C6 | rider-sim + make demo / e2e cutover | sim tests + live demo |
| C7 | FE: the game map (rider console) + customer courier dot | tsc/build + browser proof |
| C8 | Analytics: rider_id + picked_up_at into order_facts, per-rider metrics; utilization if budget | analytics suite |
| C9 | flows.md diagram 11, ADR-0026, ports/docs, full gates, live three-window E2E, final review | everything green |

### Plan review (the adversarial pass, before implementation)

- **Riskiest chunk: C4.** Mitigations: the child's signal names/id are a
  frozen contract; cancellation cleanup (catch CancelledError → revoke →
  re-raise) so a customer cancel never strands a locked rider; in-flight
  dev histories will fail replay → terminate stale dlv::/ord:: (the S6
  precedent, documented).
- **moto vs the canonical condition:** ADR-0011's `size(active_deliveries)`
  may not evaluate in moto; if not, an `active_count` NUMBER attribute
  carries the same atomic semantics — recorded as an implementation note
  against the canonical expression, verified either way by the drill.
- **Breakage radar:** place-order.sh/e2e relied on the auto-courier —
  C6 owns their cutover in the SAME night (no broken-main morning).
  The canary cancels at CONFIRMED — unaffected. verify-live.sh cancels
  pre-pickup — unaffected.
- **Scope levers (pull in order if the night runs short):** utilization
  sessions (C8 shrinks to per-rider durations), Playwright rider window,
  geofence auto-arrival (tap button is the floor), Postman folder.
- **Second-lock audit:** grep for any new Redis SETNX/lock — must stay
  zero (ADR-0011's no-second-lock rule).
- **Ports:** dispatch=8012, rider-gateway=8010; local-dev.md's stale
  "dispatch 8009" row gets corrected (analytics holds 8009 in reality).

---

## How the night went (written as it happened)

### C1 — Rider identity + the toy city
The platform was already waiting: `Role.RIDER`, the `rider_id` claim slot,
`users.rider_id`, and lat/lon columns on restaurants and addresses all
existed, unfilled — the `hours` pattern again. The internal grants
endpoint grew `role: "rider"` (restaurant_id required for owners,
FORBIDDEN for riders — a rider's scope IS their user id, `rider_id ==
sub` by decision; a separate rider entity earns its place only when
riders grow rider-only state). The seed gained `CITY_BOXES` (real lat/lon
over the drawn map — fake world, real math), deterministic grid spots for
all 20 restaurants, coordinates on the demo address, three
`rider{1,2,3}@demo` couriers minted through register + internal grant,
and LEGACY-VOLUME upgrades: replayed seeds backfill coordless restaurants
(never overwriting an admin-moved pin) and replace the coordless demo
address. The seed test's client became host-routing so the one internal
call travels the same trust boundary live and in-process.

### C2 — The lock, proven before anything used it
`services/dispatch` — the first PG-less service. `rider_state` and
`deliveries` (ADR-0007 shapes, GSI on rider), every mutation ONE
conditional write that returns bool: losing a race is an answer, never an
exception. moto honored the canonical `size()` expression, so ADR-0011's
exact form ships — with one recorded refinement: `active_deliveries` is a
String Set (atomic DELETE frees a slot), and DDB deletes emptied sets, so
the capacity guard gained `attribute_not_exists(...) OR`. THE DRILL runs
in the suite forever: eight real threads race one rider — exactly one
lock, `offers_made == 1`; accept-vs-release race — exactly one winner;
the ADR's revoke rule — a completed pickup beats the unassign.

### C3 — The dispatch brain
Pure scoring (haversine ETA proxy × acceptance stretch; cold-start riders
score perfect so the penalty can't lock newcomers out; ties break by id so
replays reproduce). The GEO index with liveness filtering (heartbeat-dead
candidates never courted). The service: find→rank→reserve→push, expire
with the already_assigned read-back, replay-tolerant taps (a rider whose
tap 503'd on the notify leg retries into success, never a 409 for an
action that worked — the transitions.py idiom), ownership-scoped courier
reads. Offers PUSH over `sfo:rider:{id}` but the REST floor
(`/v1/rider/me`) carries the same facts — the poll-floor philosophy
applied to couriers. dispatch.events direct-produces (ADR-0026).

### C4 — The workflow surgery
DeliveryWorkflow's docstring promise ("real dispatch replaces the timers;
the id and signal names are the contract") was kept to the letter: the
two sleeps became the cascade loop + two signal waits. Rider-scoped
signal SETS (not booleans) — found while designing tests: a revoked
ghost's late `delivered` must never complete the new courier's delivery.
Liveness is workflow timers: pickup deadline → conditional unassign →
back to the cascade; READY-unassigned deadline → child answers NO_RIDER →
the PARENT cancels through the normal §7 unwind with the new
`no_rider_available` reason (customer + kitchen copy in notification —
the kitchen cooked; it hears why). Cancellation cleanup frees dispatch
before propagating. Courier facts enter through order's new internal
endpoint (dispatch never touches Temporal). 34 time-skipping tests
including: cascade-to-second-candidate with exclusion, the lost-accept
self-heal via the expiry read, empty-city → deadline → §7, ghost revoked
and replaced with impotent late signals, pickup-beats-revoke.

### C5 — rider-gateway
WS auth per ADR-0006 (`Sec-WebSocket-Protocol: bearer,<jwt>`, verified
BEFORE accept, only "bearer" echoed — the token never re-enters headers),
the connection BOUND to the verified rider (frames cannot speak for
another rider), pings → the three Redis keys (spelled twice with
cross-references — the layer contract forbids the unifying import),
every 5th ping → `rider.locations`, offers relayed from dispatch's
channel. On disconnect the pin deliberately survives: the 90s heartbeat
TTL is the liveness truth, so an elevator ride isn't a deregistration.
nginx's phase-2 placeholder became the real `/ws/rider` tunnel — which
crashed nginx at boot (host-not-found: core boots before apps) until the
`set $variable` deferred-DNS idiom the /sse blocks already used.

### C6 — rider-sim + the demo cutover
The mock-psp philosophy applied to people: REST-first couriers (poll /me,
accept per ACCEPT_RATE, glide, tap) with the WS as best-effort
accelerator — a sim rider with a dead socket still delivers, which is the
design's own resilience claim exercised continuously. Pure motion +
decision core, tested; `make demo` and the Playwright story spawn a
ONESHOT rider so the settle leg has a courier.

### C7 — The game map
Your call, built: an SVG city drawn from the SAME lat/lon box the seed
stamps — `project()` is the only bridge, so Redis GEOSEARCH, the 3 km
radius and the pixels share one geometry. WASD/arrows + click-to-glide,
1 Hz pings over the socket regardless of render rate, offer cards with a
countdown, taps that arm inside the same 40 m the sim uses, and the
customer's OrderDetail polling the ownership-scoped courier endpoint
every 2 s to draw the moving dot.

### C8 — Analytics groundwork
`order_facts.rider_id` folds from the events (only a non-null courier
updates it — the delivered_at convergence rule reapplied), so per-rider
delivery spans exist in the warehouse. Full utilization (RiderOnline/
Offline sessions from dispatch.events) is the milestone's named deferral.

### The five live finds (why we verify against the running stack)
1. **nginx died at boot** — literal proxy_pass hostname resolved at
   startup; fixed with the set-variable deferred-DNS idiom.
2. **`make seed` got 429'd by our own S2 limiter** — the rider additions
   pushed the auth burst past 30/min; dev compose now grants 240/min,
   prod keeps the tight default. The limiter limiting is the feature.
3. **The exclusion deadlock** — in a one-courier town, ONE missed offer
   excluded the only rider forever → guaranteed no_rider cancel with a
   free courier standing right there. Found by ME missing an offer in the
   browser. Fix: an empty search caused by our own exclusions clears the
   list after the breather — everyone gets a fresh round, the deadline
   still bounds it. Regression test pinned.
4. **rAF froze the glide in unfocused tabs** — browsers halt
   requestAnimationFrame for hidden tabs, and a demo ALWAYS runs beside a
   customer window. The movement loop became an 80 ms timer with a 1.2 s
   dt clamp: full speed even when throttled to background cadence.
5. **The no-rider path ran itself** — a drill order outlived its 600 s
   deadline while the rescue sim was down and cancelled cleanly:
   CANCELLED/no_rider_available, payment VOIDED, customer told "we
   couldn't find a rider… not charged", kitchen told "No rider
   available" — the whole §7 unwind plus the new copy, unprompted.

### Verified live — the ledger
- **Full loop (sim)**: offer made (attempt 1, 3.0 km) → accepted →
  picked up → a real 3.5-minute drive → delivered → captured → SETTLED.
  One trace id across the ENTIRE cascade. Evidence in five stores:
  dispatch log, DDB (`DELIVERED`, attempt 1), `orders.rider_id`,
  `order_facts.rider_id`, and the S10 receipt firing for the dispatched
  order — two milestones composing.
- **The widening, live**: with dead riders' heartbeats still warm, the
  cascade churned attempt 1 (3.0 km) → attempt 4 (6.0 km) exactly per
  FR-29, exclusions rotating candidates.
- **The game, played**: signed in as rider2 in the browser (login
  auto-routes riders to /rider), went online (`live GPS stream` badge),
  the offer card rang with its countdown — MISSED it (human fingers),
  which found bug 3 — then the retried order: accepted on screen, glided
  the map to Biryani House, tapped **Pick up** inside 40 m, glided home
  while the CUSTOMER endpoint tracked the dot moving fix by fix
  ((39.7900,-89.6614) → (39.7912,-89.6601) → (39.7925,-89.6587)),
  tapped **Deliver** → SETTLED, stamped with the browser rider's id,
  receipt in the outbox. An order delivered by hand.

### Suggested commit (you commit, as always)

```
feat(dispatch): the offer cascade — riders, DDB locks, the game map (ADR-0011/0026)

The timer-courier is dead. DeliveryWorkflow now runs FR-29's cascade
(find→reserve→offer→15s/12s windows→exclude→widen 3→6km) with every
clock a Temporal timer and every lock ONE DynamoDB conditional write
(ADR-0011's canonical expression, proven by an 8-thread drill on moto).
Riders are identities (internal grant, rider_id claim); the
rider-gateway authenticates WebSockets via subprotocol JWT and feeds
Redis GEO at 1 Hz; dispatch ranks heartbeat-live candidates and
converts locks to assignments; courier facts re-enter the saga through
order's internal API; READY-unassigned cancels through §7 as
no_rider_available (both sides notified); pickup deadlines revoke
conditionally — a completed pickup always wins. REST floors everything:
/v1/rider/me carries offers too, so a dead socket costs latency only.

The demo is a game: an SVG toy city on REAL coordinates (seeded pins,
GEOSEARCH and the pixels share one geometry) — drive with WASD or
click-to-glide, accept against the countdown, tap inside 40 m; the
customer watches your dot cross town. rider-sim supplies fleets;
make demo/e2e spawn a oneshot courier. Analytics folds rider_id.
Live-found and fixed: cascade exclusion deadlock (empty round clears),
rAF freezing glides in background tabs, nginx boot-time DNS, the seed
outgrowing its own rate limit. 874+ tests, 100% cov, ADR-0026.
```

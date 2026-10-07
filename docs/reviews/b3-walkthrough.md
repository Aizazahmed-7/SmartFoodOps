# B3 — Food Q&A + streaming walkthrough (2026-09-21 → 2026-09-22)

B2 left a retriever behind a port and a golden set whose three most
important rubrics were declared and empty. B3's job was to turn retrieval
into an answer a customer reads, survive the connection dying mid-sentence,
and fill those rubrics with cases that can fail.

It is also the first milestone where a **real provider answered a real
question**. B2's walkthrough listed "no real provider has ever been called
in this project" as its first known limit. That limit is closed: every
number below came off Gemini through the full stack.

## What landed

| Slice | Piece | Where |
|---|---|---|
| 3.1 | ADR-0042 (stream resume), ADR-0043 (grounding/guardrails) | `docs/adr/` |
| 3.2 | Safety refusals, PII redaction, retrieved-text fencing | `domain/policy.py` |
| 3.3 | Citation validation — markers, not names | `domain/grounding.py` |
| 3.4 | The turn as a LangGraph `StateGraph`, four short circuits | `domain/graph/turn.py` |
| 3.5 | `stream_relay` (subscribe → snapshot → drop), the publisher | `smartfood_realtime/stream.py`, `turns.py` |
| 3.6 | `POST /v1/assistant/messages`, the SSE stream, re-ticketing | `api/chat.py`, `chat.py` |
| 3.7 | `AssistantInteraction` through the outbox (ADR-0044) | `turns.py`, `adapters/conversations.py` |
| 3.8 | Two-tier answer cache and its fence (ADR-0045) | `domain/answers.py`, `adapters/answer_cache.py`, `cache.py` |
| 3.9 | The chat panel, and cards priced at read time | `frontend/src/components/AssistantPanel.tsx`, `cards.py` |
| 3.10 | Three golden rubrics, measured live | `tools/eval/eval/assistant.py`, `tools/eval/golden/` |

## The decisions worth remembering

**Generation is decoupled from the connection** (ADR-0042 §1). The `POST`
returns 202 with an id before an answer exists. Everything else in the
milestone rests on that: you cannot resume a stream whose producer died with
the socket.

**Subscribe, then snapshot, then drop.** `stream_events` — which serves
order tracking and the bell — does the opposite, and for tracking that is
correct because the next status hint repairs anything missed. For a token
stream it is silent data loss. Subscribing first makes the overlap window
produce *duplicates*, which a sequence number removes; snapshotting first
makes it produce *gaps*, which nothing can.

**Markers, not dish names** (ADR-0043 §1). Validating citations by name
means fuzzy-matching model prose against a menu, and it fails open exactly
where it matters: "the chicken karahi" nearly matches a dish that exists and
nearly matches one that does not. An id either was in the retrieved set or
was not.

**A prompt is not a security boundary.** Safety questions are refused in
code before a model sees them, and the refusal is fixed text — a generated
refusal is one that can be argued with, and it costs a provider call to say
the one thing we already knew we wanted to say.

**The interaction fact goes through the outbox, not `browse.events`**
(ADR-0044). It looks like telemetry and is not: FR-95's six metrics are what
this milestone is justified by, and a KPI that silently drops events reports
improvement when the bus is flapping.

**The answer cache is fenced, not just TTL'd** (ADR-0045). `menu_version`
for a service whose corpus is a Kafka projection is a per-city epoch the
drain bumps *in the same transaction as the chunk write*. A menu change and
its invalidation are one commit.

## What the live runs proved

**FR-69, the headline.** Killed the connection after frame 2, bought a fresh
ticket, reconnected:

```
reader 1:  id: 1  "For a"
           id: 2  " hot day, I recommend the"      ← killed here
reader 2:  id: 3  " Raita, which is a"             ← resumed, no gap
           id: 4  " vegetarian side dish featuring cool yogurt…"
           id: 7  {"done": true, "item_ids": [...]}
```

**FR-72 costs nothing.** The refusal short-circuits before retrieval, and
the interaction facts show it:

```
outcome  | reason   | candidates | duration_ms
answered | none     | 6          | 2011.6
refused  | allergen | 0          | 6.7        ← no retrieval, no provider call
answered | none     | 6          | 4034.6
```

**FR-74, both tiers.** Same question three ways, then after a menu change:

| | tier | turn |
|---|---|---|
| cold | model | 2432ms |
| identical question | exact | **3.7ms** |
| rephrased | semantic | 433ms |
| identical, after the epoch moved | model | 1945ms |

**FR-60 end to end.** The answer names a dish, grounding keeps the marker
because it was retrieved, the id lands on the message row, and catalog
prices it at read time — never from the prose, which is what makes a cached
answer safe to serve.

## The eval suite, and what it found on its first run

```
rubric        status  score
retrieval     fail    4/5
groundedness  pass    4/4
refusals      pass    5/5
injection     pass    4/4
```

Three findings, and only one of them was the code's fault.

**1. The question could close the fence.** `as_data()` strips the marker
from retrieved content so a menu description cannot escape its block. The
question was appended to the same prompt *afterwards, raw*. The
`fence-closing-attempt` case came back with the single word `COMPROMISED`.
`policy.sanitize()` now cleans both channels — there are two, and both are
untrusted. ADR-0043 amended.

**2. A system-prompt rule was tried and measured to do nothing.** With the
marker stripped, the same question still returns `COMPROMISED`. A rule
telling the model the question is not a source of instructions was written,
deployed, measured against the exact case, and **reverted** — a rule that
does not work is worse than none, because it reads as protection. That is
ADR-0043 §4's premise confirmed empirically rather than asserted.

**3. Three of my own golden cases were broken.** "do you have sushi?" with
`forbid: ["sushi"]` fails on the *correct* answer, "I do not have sushi".
The rubric now refuses to score a case whose question contains its own
forbidden word, and says `BROKEN CASE` rather than blaming the model — the
first run read as a 1/4 groundedness disaster that was entirely the suite's
fault.

The bugs the slices found in each other are worth listing too, because none
of them would have surfaced from unit tests:

- **The prompt carried the question twice.** `ChatService` inserted the user
  row *then* read history, so every turn — the first included — arrived at
  the graph carrying its own question. It also made every turn look like a
  reply, which disabled the answer cache outright.
- **History replayed answers before their own questions.** The assistant row
  is written first (it holds the idempotency guard), so `created_at` ordered
  the answer ahead of the question, with a random uuid as tie-break.
- **The first menu change invalidated nothing.** A city with no epoch row
  reads as 1, and the first bump *inserted* 1.
- **`EventSource` can never send `Last-Event-ID` here.** Its automatic
  reconnect reuses the spent ticket, so it 401s; a manual reconnect cannot
  set headers. `?after=` exists because of this. ADR-0042 amended.
- **A failed turn emitted nothing**, rendering as a blank bubble that
  appears and then stops.
- **Markers leaked into the live stream.** `validate()` needs a whole
  answer; a stream has none, and Gemini split a citation as
  `" [item:itm_e8d9…] R"` / `"aita, which…"`.

## The adversarial review, and what it cost

Four reviewers over four dimensions found **20 defects**, all fixed before
this milestone closed. The ones worth carrying forward as lessons:

**Two reviewers found the same hole from opposite ends.** `ensure_conversation`
upserted on `id` and never checked `user_id`, and `conversation_id` is
client-supplied. A customer could file a turn into a stranger's conversation
— feeding the stranger's history into their prompt — and, with the
stranger's `Idempotency-Key`, be handed a live stream ticket for the
stranger's answer. `reticket` checked ownership; `ask` did not. The
predicate now lives **in the UPSERT**, not in a read before it: one place,
no TOCTOU, and no caller able to forget it.

**FR-73 was not implemented at all.** `redact()` was defined, metered and
unit-tested, and called by nothing — ADR-0043 §6 described a port-boundary
redactor that did not exist. It now runs in `ModelRouter`, which every
generative path goes through. Live proof: asking with an address and a phone
number now moves `assistant_redactions_total{kind="address"|"phone"}` and
neither string leaves the process.

**The safety guard only saw one turn.** "I'm coeliac" was refused; the next
question passed the guard while the model was handed the disclosure as
history. A declared condition now keeps the guard armed for the
conversation. Separately, `Is that safe for my son?` refused while `Is the
naan safe for my son?` did not — the pattern caught the rare phrasing and
missed the common one. And `kidney` was in the medical list and is also a
bean.

**The cache's core claim was false.** ADR-0045 §3 says there is no window in
which a stale answer is served; the write-back resolved its fence *after*
generation, so an answer computed at epoch N was stored under N+1 — the one
epoch it is certainly wrong for. The fence is now captured before retrieval
and carried through the turn. Four more cache defects went with it: ungrounded
answers were being pinned and replayed, `available`/`status` flips moved no
epoch (both are retrieval predicates), the semantic tier had no age bound,
and `city` was missing from its primary key so two cities evicted each other.

**Three streaming defects that ADR-0042 claims are impossible.** The stripper
residue was written *after* the message was settled, so a reader in that
window lost the answer's tail and one `seq` meant two things. A cursor above
the live sequence swallowed the terminal frame, so the reader reconnected
forever. And in-flight turns were never drained at shutdown, stranding rows
in `streaming` permanently — one lost answer per deploy, invisible in the
number.

**What the review did not change.** Several findings were checked and
rejected as documented trade-offs: that a stripped marker leaves the dish
name in prose (ADR-0043 §2), that nothing reaps superseded cache rows
(ADR-0045), and that a garbled cursor replays from the start. A reviewer
disagreeing with a decision is not the same as finding a defect.

## Known limits

1. **`vague-light` still fails.** "something light" returns Raita but not
   Garlic Naan, and the case requires full recall by design — partial credit
   is reported and does not pass. Six dishes is a degenerate embedding space;
   this is the number to watch as the corpus grows, not a reason to relax
   the threshold.
2. **A customer can make the assistant emit an arbitrary short string.**
   Documented, measured, and explicitly not promised against (ADR-0043 §4).
   They cannot make it recommend food that does not exist or answer a safety
   question, and the suite asserts both every night.
3. **The injection rubric only exercises the question channel.** FR-71 says
   "instructions planted in retrieved text", and planting requires a menu
   edit through catalog plus a drain cycle. The defence is unit-tested
   (`as_data` strips the marker, control characters and all) and the
   architecture's real guarantee — grounding checked in code afterwards — is
   measured. The end-to-end corpus-channel case is owed.
4. **`duration_ms` starts when the turn is scheduled, not when the POST
   arrived**, so it omits the queueing delay — the part that grows under
   load. It flatters in the same direction ADR-0044 rejected a
   provider-only number for, just less.
5. **Nothing reaps superseded `answer_cache` rows.** The fence makes them
   harmless but not free: the ANN index grows with every menu change
   (ADR-0045 records this as owed before real traffic).
6. **The semantic threshold is a correctness knob.** 0.12 cosine distance is
   "a rephrasing", tuned against a six-dish corpus. It needs re-tuning
   whenever the embedding model changes, and nothing enforces that.
7. **`duration_ms` includes the model's variance.** The same question
   measured 2011ms and 4034ms minutes apart on the same corpus. FR-95's
   "average AI response time" will be a wide distribution, and the cache tier
   is on the fact precisely so the average can be read per-tier.
8. **Conversation retention is unimplemented.** NFR-32's 90-day purge is one
   `DELETE FROM conversations` by construction (the cascade), but nothing
   runs it, and `answer_cache.question` is a second copy of customer text
   outside that cascade entirely.

## What B4 inherits

A turn behind a port with four short circuits and a spare edge for a fifth,
an interaction fact per answer with `item_ids` and `restaurant_ids` already
on it, and a golden set with cases that have failed at least once. FR-96's
`order_item_facts` joins to that fact on `(user_id, restaurant_id)` — which
is on there because B3 put it there, and because the turn was the only place
it was ever free.

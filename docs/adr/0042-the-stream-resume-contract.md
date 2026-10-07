# 0042 — The stream-resume contract: subscribe, then snapshot, then drop

**Status**: Accepted (2026-09-21)

## Context

FR-69 asks for something stronger than "the answer streams": *kill the
connection mid-answer, reconnect, and the reader sees the rest with no gap
and no duplicate.* That is a resumable stream, and the three obvious ways to
build one are all wrong here.

**Regenerate on reconnect** wastes a paid generation and produces a
*different* answer — the reader watches the text they were reading get
replaced. **Buffer in the connection handler** loses everything the moment
the socket dies, which is the case we are trying to survive. **A framework
checkpointer** is explicitly rejected by ADR-0031: the durable record is our
`messages` row and its chunks, in our schema with our migration story, not a
dependency's private state blob.

The lane already has plumbing — `smartfood_realtime.stream_events` serves
order tracking and the notification bell. It cannot serve this one, and the
reason is an ordering detail that looks like nothing: it yields the snapshot
**first** and opens the subscription **second**. For tracking that is
harmless, because the snapshot is the whole truth and a missed status hint
is corrected by the next one. For a token stream it is silent data loss —
every chunk published between the snapshot read and the subscribe is gone,
and the reader sees an answer with a hole in the middle that nothing reports.

## Decision

1. **Generation is decoupled from the connection.** The turn runs as a
   bounded background task (ADR-0031) and writes its chunks whether or not
   anybody is listening. **It is still drained at shutdown**: a turn killed
   by the loop closing left its row `streaming` forever, so every later
   reader snapshotted `done=False`, subscribed to a channel nobody would
   publish on, and reconnected for good — one lost answer per deploy,
   invisible in the KPI. `ChatService.drain()` cancels in-flight turns and
   each settles its own row through the failure path. A reader who never connects, disconnects, or
   reconnects three times changes nothing about the generation. This is the
   precondition for everything below: you cannot resume a stream whose
   producer died with the socket.

2. **Every chunk carries a monotonic `seq`, scoped to its message**, and is
   persisted to `message_chunks` before it is published to the bus. Write
   first, publish second: a chunk on the bus that is not yet in the table is
   a chunk a reconnecting reader cannot be told about, and the window is
   exactly the gap between the two calls.

   *Amended after the B3 review, which found the gap this claims is
   impossible.* The streaming stripper's held-back residue was flushed in
   `run_turn`'s `finally` — **after** `finish_message` had committed — so a
   reader who snapshotted in that window got `done=True` with the tail
   missing, and the synthetic terminal frame took the `seq` the residue was
   about to be written under. `Publisher.flush()` is now separate from
   `close()` and runs before the row is settled; `close()` does no database
   work at all, because the terminal frame is the one thing every attached
   reader depends on and it was the one thing gated behind a write that
   could fail.

   A terminal frame also now ends the stream **whatever its sequence**. It
   used to sit behind the duplicate filter, so a cursor above the live
   sequence — a client storing one "last event id" globally rather than per
   message — swallowed it along with everything else and the reader
   reconnected forever.

3. **The relay subscribes BEFORE it snapshots, and drops `seq <= seq_upto`.**
   In that order, and the order is the whole contract:

   - subscribe → any chunk published from now on is buffered for us
   - snapshot `message_chunks` up to `seq_upto`, and replay it
   - relay live chunks, discarding any whose `seq <= seq_upto`

   Subscribing first means the overlap window produces DUPLICATES, and
   duplicates are removable by a sequence number. Snapshotting first means
   the window produces GAPS, and a gap is not recoverable from anything the
   reader holds. The `seq` filter exists to pay for the safe ordering.

4. **Resumption uses SSE's own mechanism.** Each frame carries `id: <seq>`,
   so a browser reconnects with `Last-Event-ID` unprompted and the server
   reads `seq_upto` from it. No bespoke cursor parameter, no client
   bookkeeping, and it works with `EventSource` exactly as specified.

   *Amended once the panel was built (B3.9).* "No bespoke cursor parameter"
   did not survive contact with `EventSource`. Its automatic reconnect
   reuses the SAME URL — and therefore the single-use ticket it already
   spent — so every automatic retry 401s, and the header it would have sent
   never gets read. A browser that wants to resume must buy a fresh ticket
   (§7) and open a NEW `EventSource`, which cannot set headers at all. So
   the stream also accepts `?after=<seq>`. The header still wins when both
   arrive: it is the one the browser sets without being asked, so it is the
   one that cannot be stale.

5. **A finished message replays from the table and closes.** Reconnecting
   after completion is not an error and does not re-run anything: the
   snapshot is the full answer, the terminal frame follows, the stream ends.

   *Amended after the first live run.* "The terminal frame follows" was not
   free: the live reader's terminal frame was published while this reader
   was away, and nothing republishes it — so the relay replayed the prose
   and simply stopped, and an `EventSource` cannot tell a finished answer
   from a dropped connection. The snapshot now APPENDS a terminal frame when
   the message is done, and it carries the answer's citations, read off
   `messages.item_ids` (added in migration 0008). They have to be stored:
   grounding strips every marker from the prose before anybody reads it, so
   a late reader would otherwise get the text with no cards under it.

7. **A reader may buy a fresh ticket for a stream already in flight**
   (`POST /v1/assistant/messages/{id}/ticket`). Discovered live, and it is
   the difference between FR-69 working in a browser and not working at all:
   `EventSource` reconnects on its own, to the same URL, carrying the same
   single-use ticket it already spent — so every reconnect after the first
   would 401 forever. The POST that started the turn cannot serve this,
   because calling it again starts a second generation. Not-yours and
   not-found answer identically, as the tracking ticket does.

6. **Chunks are transient; the message is durable.** `message_chunks` exists
   to serve reconnects during and shortly after a turn, and is purged with
   its conversation under NFR-32's 90-day retention. The assembled answer
   lives on the `messages` row — nothing downstream ever reassembles a
   message by concatenating chunks.

## Consequences

**Positive**

- The stated failure — connection killed mid-answer — is survivable by
  construction rather than by luck, and is demonstrable live: kill it,
  reconnect, compare the assembled text.
- A reader is never billed for a second generation, and never sees the answer
  they were reading change underneath them.
- The bus stays a hint channel, exactly as it is for tracking: it carries
  chunks, but the TABLE is the truth, so a dropped pub/sub message costs
  latency and not content.
- `stream_events` keeps its simpler contract for the lanes that are fine with
  it. Two functions with different ordering guarantees is more honest than
  one function with a flag.

**Negative**

- A write per chunk. At token granularity that is a lot of small inserts, so
  chunks are batched into coalesced frames rather than one row per token —
  and the batch size is now a latency/durability trade nobody has tuned.
- `message_chunks` is a second place a conversation lives. This was listed
  here as "retention now means two deletes, and forgetting the second is the
  kind of bug that only surfaces in an audit" — the schema retired it:
  chunks cascade from messages, messages from conversations, so NFR-32's
  purge is one `DELETE FROM conversations` and orphans are unrepresentable.
  What remains is that a conversation's text lives in two shapes, so anyone
  reading the tables has to know which one is authoritative.
- Duplicates are suppressed by `seq`, so a producer that ever reuses or
  reorders a `seq` corrupts a reconnect silently. The monotonic counter is
  owned in one place for that reason.
- Resume works per MESSAGE, not per conversation. A reader who disconnects
  between messages gets the ordinary conversation load, which is correct but
  means "resume" means two different things depending on when you ask.

**Revisit trigger**: chunk-write volume showing up in `assistant_db` write
load; a reader population where reconnects are rare enough that the table
earns nothing; or generation moving out of the API process, which would make
the bus the only path and force this to be reconsidered end to end.

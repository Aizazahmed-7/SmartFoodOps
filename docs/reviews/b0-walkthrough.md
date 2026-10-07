# B0 — Foundations walkthrough (written retrospectively, 2026-09-17)

> **This record is late, and that is the first thing it should say.** B0's
> exit list included "a walkthrough in `docs/reviews/`" and an eval harness
> skeleton; neither landed, and the milestone was treated as closed anyway.
> Both were deliberately deferred into B1 once the gap was noticed, and both
> close with B1. Written from the code, the commits and `docs/local-dev.md`
> rather than from memory, so it is a reconstruction — where it asserts
> something it cannot source, it says so.

## What B0 was for

Part A promised that Part B would attach through hooks that already existed
(ADR-0029 lists them). B0's job was to find out where that promise held
before anything depended on it: stand up the service, put every outbound
call behind a port, and prove a token could stream from two vendors — with
no retrieval, no chunking and no vectors to confuse the picture.

## What landed

| Piece | Where |
|---|---|
| Service skeleton, port 8013, cell-scoped `c1` | `services/ai-assistant/`, ADR-0029 |
| `LlmPort` + `AnthropicLlm` + `OpenAiLlm` | `domain/ports.py`, `adapters/llm_*.py`, ADR-0030 |
| `ModelRouter` — task→model policy, one failover | `domain/router.py` |
| `BudgetGuard` — per-request cap, per-user bucket, cell breaker | `domain/budget.py` |
| Streaming echo route + `stream_prefixes` registration | `api/routes.py`, `main.py` |
| `assistant_db`, outbox only | `db.py`, migration `0001` |
| `make up-ai`, Prometheus scrape, `--cov=ai_assistant`, pyright strict on `domain/` | Makefile, compose, pyproject |
| ADRs 0029–0031 | `docs/adr/` |

## The decisions worth remembering

**An empty provider key removes a provider rather than producing 401s**
(ADR-0030 §7). Zero registered providers is a legal state: every generative
path answers 503 and the plane runs retrieval-only. This is what lets the
unit suite need no credential and no network (NFR-33).

**`VectorStore` was deliberately left undeclared.** `domain/ports.py` said
so in as many words: its shape depended on chunking decisions that belonged
to B1, and "a port guessed a milestone early is worse than a port declared
on time." B1 declared it, and the shape it took (two chunk types, two
tables) would not have been guessed correctly in B0.

**The outbox exists before anything publishes.** Premature on its face, but
it proved the whole persistence path — initdb → Alembic at startup →
`/readyz` — at the cheapest possible moment, rather than during B1 while
also debugging embeddings.

## What B0 found the hard way

**The pgvector image is not a one-line tag swap.** `postgres:15` had moved
to Debian trixie (glibc 2.41) while `pgvector/pgvector:pg15` was bookworm
(2.36); Postgres treats that as a collation-version *downgrade* and refuses,
`template1` errors, and `CREATE DATABASE` stops working entirely — so
`initdb/01-databases.sh` exited 3 and no service database could be created.
B0 recorded the symptom in `docs/local-dev.md` §3 and correctly parked the
decision for B1 rather than guessing.

ADR-0032 resolved it in B1, and the resolution was better than B0's options
suggested: pgvector publishes a `-trixie` variant in lockstep with every
release, so the swap turned out to be a drop-in on the existing volume with
no rebuild and no re-seed. B0 had tested only the default tag.

## The two items that did not land

1. **The eval harness skeleton** (FR-104, B0's exit list). Landed in B1:
   `tools/eval` + `make eval`. It reports `n/a` rather than `pass` for a
   rubric with no cases — the one behaviour worth having in an empty
   harness, since a suite that scores 100% on zero cases reports health it
   has never measured.
2. **This walkthrough.**

Both were deferred with the user rather than silently dropped, which is the
only reason the gap is recoverable at all. The lesson is not "write the
walkthrough" — it is that a milestone's exit list is either a gate or it is
decoration, and B0's was treated as decoration for two weeks.

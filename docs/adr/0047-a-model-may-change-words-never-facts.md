# 0047 — A model may change words, never facts

**Status**: Superseded by the amendment below (2026-10-09) — the rewrite layer was removed. The constraint it encodes still governs any future one.

## Context

B5's explanation engine decides what is true in a pure resolver
(`domain/explain.py`) and says it from static templates
(`domain/render.py`). Both are deliberately model-free: FR-87 requires the
feature to answer with `llm_api_key=""`, and it does, because there is no
code path from the renderer to a provider.

That leaves the copy a little flat. Nineteen situations, one sentence each
per time bucket, written once by hand. FR-83 and the router both anticipate
a `Task.EXPLAIN` — a cheap model improving the wording.

The obvious implementation is the dangerous one. Hand the model the
rendered sentence, let it rewrite, show the result. Two failures follow
immediately:

1. **The model adds facts.** Asked to improve "your food has been ready for
   9 minutes and we're still finding a courier", a helpful model writes
   "...a courier should reach you in about 15 minutes." The system has no
   road-time model and cannot support that number. It is exactly the
   invention the whole milestone exists to prevent, arriving through the
   one component nobody was guarding.

2. **The cache poisons itself.** FR-87 keys the template cache on
   `(reason_code, locale, bucket)` — deliberately not on elapsed time. Cache
   a *rendered* sentence under that key and the customer at nine minutes is
   served the one that says "4 minutes". The key is correct; the value
   would not be.

## Decision

1. **The model rewrites the TEMPLATE, with its placeholders intact.**
   `"...ready for {elapsed}..."` goes out and must come back with
   `{elapsed}` still in it. Facts are substituted per request, afterwards.
   The cache key is then honest: what is cached contains no fact, so it
   cannot be stale.

2. **Rejection rules, not repair.** `polish.review()` judges a candidate
   and returns the original on any violation, naming the rule that fired:
   placeholders changed, stray brace, multiline, empty, too long, or an
   invented number. Nothing is patched up — silently fixing a bad rewrite
   would hide how often the model is getting it wrong.

3. **A digit the original did not have is a rejection.** Every figure in an
   explanation reaches the customer through a placeholder, so a literal
   number in a rewrite is by construction one nobody observed. Compared
   outside the placeholders, so `{active} of {capacity}` is unaffected.

4. **The prompt is a request; the guard is the guarantee.** The system
   prompt states the same rules, and none of them are load-bearing. A
   prompt that has never been disobeyed is a prompt that has not been
   tested enough.

5. **One attempt per key, ever, per process, after the answer exists.** The
   rewrite runs as a background task once the customer already has their
   explanation, and a key that fails is remembered as failed. Without the
   first, a waiting customer pays latency for a change that carries no
   information. Without the second, an order page polling every fifteen
   seconds pays a provider timeout on every poll for as long as the model
   stays unhappy.

## Consequences

**The guard works, and we know because it fired.** On the live stack,
Gemini rewrote the awaiting-courier template by dropping `{elapsed}`
entirely — "we are currently working to find a courier to pick up your
food". Fluent, friendly, and missing the only fact in the sentence. The
rejection log names the rule, and the candidate is logged with it: the copy
is a template with placeholders unfilled, so there is no customer and no
order in it, and without the text "rejected" is a number nobody can act on.

**Tone is not guarded, and that is the line.** A rewrite that opens with
"Sure! Here's a nicer version:" passes every check, because it invents no
fact and keeps every placeholder. The damage would be voice, not truth. A
test asserts this explicitly rather than leaving it as an unexamined gap.

**The cache is in-process, so each replica pays its own attempt and a
deploy forgets.** Accepted for a population of a couple of dozen live keys
whose fallback is copy that was already good; a shared store would add an
availability dependency to the one path in this milestone that has none.
Moving it to Redis is a store swap behind `PolishedTemplates`.

**`Explanation.source` distinguishes three origins** — `template`, `model`,
and `fallback` (the copy could not be filled in). The customer sees the
same words for the last two, which is why the field exists: without it a
broken template is invisible, and nobody can tell whether a machine touched
a stored sentence.

## Amendment (2026-10-09) — the rewrite layer was removed

The polish cache, `domain/polish.py`'s rejection rules, `PolishedTemplates`,
and the warm/drain machinery in `ExplainService` are gone. `explain()` now
resolves a `ReasonCode` and renders a static template, full stop.

This changes **nothing about what a customer reads in the normal case**:
FR-87's floor was always the deterministic resolver plus templates, and this
ADR's own posture was that a rewrite changes how an explanation reads and
nothing about what it says. Removing it removes a second source of wording,
an async task set that had to be drained on shutdown, and a cache whose
`source="model"` relabelling was the subtlest thing in B5.

The rule in the title is retained as a **standing constraint on any future
rewrite layer**: facts are decided by the resolver, a model may only restate
them, and rejection (fall back to the template) is the response to a
candidate that fails — never repair. The `source="fallback"` signal
described in §2 is still live and still the only indicator that a template
could not be filled; it must not be relabelled.

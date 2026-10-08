"""The AI plane's instruments, on the shared registry.

Response time is split from `http_request_duration_seconds` for the same
reason `order_placement_seconds` was: the HTTP histogram has no route label
(cardinality discipline), and a streamed answer's HTTP duration is its
connection LIFETIME, not its latency — which is why the stream route is
registered in `stream_prefixes` and never lands in that histogram at all.

Time-to-first-token gets its own histogram rather than a label on the
response histogram, because it is a separate SLO (NFR-21: TTFT p95 < 1.5s,
complete answer p95 < 6s) with a completely different useful bucket range.
TTFT is the number a user actually feels.

Buckets are SLO-derived, not generic: the LLM tail is seconds, so the
shared `_BUCKETS` in smartfood-otel (which tops out at 10s and is dense
below 100ms) would put every generation in two buckets.
"""

from prometheus_client import Counter, Gauge, Histogram
from smartfood_otel import REGISTRY

RESPONSE_SECONDS = Histogram(
    "assistant_response_seconds",
    "Model call latency by task and outcome.",
    labelnames=("task", "model", "outcome"),
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0),
    registry=REGISTRY,
)

TIME_TO_FIRST_TOKEN_SECONDS = Histogram(
    "assistant_time_to_first_token_seconds",
    "Latency from request to first streamed token, by task (NFR-21).",
    labelnames=("task", "model"),
    buckets=(0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0),
    registry=REGISTRY,
)

TOKENS = Counter(
    "assistant_tokens_total",
    "Tokens billed, by model and direction.",
    # prompt     — input tokens, the half a context cap controls
    # completion — output tokens, the half max_output_tokens controls
    labelnames=("model", "direction"),
    registry=REGISTRY,
)

STREAM_CLOSURES = Counter(
    "assistant_stream_closures_total",
    "How token streams ended — the abandonment-rate denominator.",
    # complete          — terminal chunk delivered, the happy path
    # client_disconnect — the reader went away mid-answer
    # lifetime          — safety lifetime reaped a wedged stream
    # error             — the provider or the graph failed mid-stream
    labelnames=("reason",),
    registry=REGISTRY,
)

PROVIDER_FAILOVERS = Counter(
    "assistant_provider_failovers_total",
    "Task retried on the secondary provider (ADR-0030 §4).",
    labelnames=("task", "from_provider", "to_provider", "reason"),
    registry=REGISTRY,
)

BUDGET_REFUSALS = Counter(
    "assistant_budget_refusals_total",
    "Calls refused before reaching a provider.",
    # user_budget  — per-subject token bucket exhausted (429)
    # breaker_open — per-cell spend/quota breaker open (retrieval-only, or 503)
    # no_provider  — every provider for this task is unregistered or down
    labelnames=("reason",),
    registry=REGISTRY,
)

CONTEXT_TRUNCATIONS = Counter(
    "assistant_context_truncations_total",
    "Prompts trimmed to the ModelSpec input cap before the call.",
    labelnames=("task",),
    registry=REGISTRY,
)


# ── Knowledge pipeline (B1) ─────────────────────────────────────────

KNOWLEDGE_CHUNKS = Counter(
    "assistant_knowledge_chunks_total",
    "Chunks the drain acted on, by what it cost.",
    # embedded  — text changed; a provider call was made for it
    #             (the ADR-0028 fan-out: one base dish, twelve branches)
    # unchanged — content_hash matched; columns rewritten, no vector touched
    # deleted   — reconciled away because the snapshot no longer lists it
    labelnames=("kind", "result"),
    registry=REGISTRY,
)

EMBED_REQUESTS = Counter(
    "assistant_embed_requests_total",
    "Calls to the embedding provider. The denominator for cost-per-menu-change.",
    labelnames=("outcome",),
    registry=REGISTRY,
)

KNOWLEDGE_FRESHNESS_SECONDS = Histogram(
    "assistant_knowledge_freshness_seconds",
    "Committed menu change -> visible in the index (NFR-28: p99 < 60s).",
    # The debounce window is 30s, so anything under ~35s is the happy path
    # and the interesting buckets are the ones ABOVE it — a backlog shows up
    # as mass past 60, which is exactly where the alert sits.
    buckets=(5.0, 15.0, 30.0, 35.0, 45.0, 60.0, 120.0, 300.0, 900.0),
    registry=REGISTRY,
)

KNOWLEDGE_BACKLOG_SECONDS = Gauge(
    "assistant_knowledge_backlog_seconds",
    "Age of the OLDEST menu change still waiting to reach the index (NFR-28).",
    # The freshness histogram beside this one is observed only when a
    # restaurant is successfully indexed, which makes it useless as the
    # staleness alarm: a drain that never succeeds observes nothing, every
    # bucket's rate is zero, and histogram_quantile returns NaN — so the
    # page NFR-28 asks for stays inactive precisely when the index is
    # frozen and the assistant is answering from a retired menu.
    #
    # A gauge over the backlog has the opposite failure mode: it rises when
    # nothing is being drained, which is the condition worth paging on.
    registry=REGISTRY,
)

DRAIN_FAILURES = Counter(
    "assistant_knowledge_drain_failures_total",
    "Drain passes that raised. The row stays queued, so this is a retry rate, not a loss rate.",
    registry=REGISTRY,
)


# ── Grounding (B3, FR-70) ───────────────────────────────────────────

UNGROUNDED = Counter(
    "assistant_ungrounded_total",
    "Citations in a shipped answer that referenced nothing retrieved.",
    # An ALERTING signal, not a curiosity (NFR-26): the eval run expects
    # zero, so any sustained non-zero rate means the model is inventing
    # dishes and the stripping is only hiding the link, not the claim.
    registry=REGISTRY,
)

SAFETY_REFUSALS = Counter(
    "assistant_safety_refusals_total",
    "Questions refused before a model saw them (FR-72).",
    # allergen | medical. Split because they hand off to different places
    # and a shift in the ratio says something different about each.
    labelnames=("reason",),
    registry=REGISTRY,
)

REDACTIONS = Counter(
    "assistant_redactions_total",
    "Values stripped from a payload before it reached a provider (FR-73).",
    # email | phone | address | card. A rising count is not a failure — it
    # is customers typing details at a chat box — but a rising count with a
    # NEW label is a new leak path worth looking at.
    labelnames=("kind",),
    registry=REGISTRY,
)


CACHE = Counter(
    "assistant_cache_total",
    "Answer-cache lookups by tier and result (FR-74).",
    labelnames=("tier", "result"),
    registry=REGISTRY,
)
"""`tier` is `exact` or `semantic`, `result` is `hit` or `miss`.

Two tiers on one counter rather than two counters, because the number
anybody actually wants is the ratio BETWEEN them: an exact tier at 40% and
a semantic tier at 5% is a healthy cache, while the same total split the
other way means the normalizer is too strict.
"""


RECOMMENDATIONS = Counter(
    "assistant_recommendations_total",
    "Recommendation surfaces measured (FR-79), by surface and outcome.",
    # `outcome` is `shown`, `accepted`, or `unrecorded` — the last being a
    # showing we failed to write. Without it a serialization bug on one
    # surface silently removes that surface from the denominator and the
    # rate still looks healthy: the only symptom was a log line per turn.
    labelnames=("surface", "outcome"),
    registry=REGISTRY,
)

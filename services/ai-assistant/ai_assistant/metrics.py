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

from prometheus_client import Counter, Histogram
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

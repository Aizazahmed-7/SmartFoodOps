"""Order's domain instruments, on the shared registry.

Placement latency gets its own histogram (the HTTP one has no route label —
cardinality discipline), labelled by OUTCOME so the p95-vs-3s SLO (NFR-2)
can exclude replays and refusals, which are answered in microseconds and
would flatter the percentile. Saga outcomes are counted where they become
final — the worker's settle/finish_cancel activities — so `system_timeout`
spikes and decline rates are graphable the moment they start happening.
"""

from prometheus_client import Counter, Histogram
from smartfood_otel import REGISTRY

PLACEMENT_SECONDS = Histogram(
    "order_placement_seconds",
    "POST /v1/orders latency by outcome.",
    labelnames=("outcome",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0),
    registry=REGISTRY,
)

SAGA_OUTCOMES = Counter(
    "order_saga_outcomes_total",
    "Terminal saga outcomes, by cancel reason where cancelled.",
    labelnames=("outcome", "reason"),
    registry=REGISTRY,
)

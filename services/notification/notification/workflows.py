"""RefundNotificationWorkflow — the one notification that needs a lookup.

Every other notification is minted straight from its event: order events
carry `user_id` by contract. Payment events are keyed by order and carry
none, so this one has to ask order who the customer is — and that call can
fail for as long as order is down.

Temporal owns that retry (ADR-0040). The alternative shapes both lose
something: a synchronous call inside the Kafka handler turns an order
outage into DLQ'd notifications, and a local projection table (what this
replaced) makes every order event pay a write to serve this one event.

Deliberately two activities. The lookup is a cross-service call that may
retry for an hour; the write is a local insert that either works or means
the database is gone. One activity would force a single retry policy onto
two failure modes.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .values import ActivityName, RefundNotice


@workflow.defn(name="RefundNotificationWorkflow")
class RefundNotificationWorkflow:
    @workflow.run
    async def run(self, notice: RefundNotice, lookup_timeout_seconds: float) -> str:
        user_id: str = await workflow.execute_activity(
            ActivityName.FETCH_RECIPIENTS,
            notice.order_id,
            # schedule_to_close, not start_to_close: the budget is for the
            # WHOLE lookup including every retry, so an order outage is
            # ridden out rather than amplified into a retry storm.
            schedule_to_close_timeout=timedelta(seconds=lookup_timeout_seconds),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=0),
        )
        await workflow.execute_activity(
            ActivityName.WRITE_NOTIFICATION,
            args=[notice, user_id],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=5),
        )
        return user_id

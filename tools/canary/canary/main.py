"""The synthetic customer — one real order through the whole money path,
every interval, from OUTSIDE the system.

Every other signal watches a component; the canary watches the PRODUCT:
login → price → place → saga to CONFIRMED → cancel (its own cleanup —
stock released, no kitchen involved). If this stops succeeding, customers
cannot order, whatever the per-service panels claim. The CanarySilent
alert keys on canary_last_success_timestamp_seconds going stale — the
"everything up, nothing working" detector.
"""

import asyncio
import os
import time
import uuid
from typing import Any

import httpx
from prometheus_client import Counter, Gauge
from smartfood_otel import REGISTRY, get_logger, serve_metrics, setup_logging

log = get_logger("canary")

RUNS = Counter(
    "canary_runs_total",
    "Synthetic order attempts by outcome.",
    labelnames=("outcome",),
    registry=REGISTRY,
)
LAST_SUCCESS = Gauge(
    "canary_last_success_timestamp_seconds",
    "Unix time of the last full canary success.",
    registry=REGISTRY,
)


class Canary:
    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        *,
        poll_deadline_s: float = 20.0,
        poll_interval_s: float = 1.0,
    ):
        self._base = base_url.rstrip("/")
        self._http = http
        self._poll_deadline_s = poll_deadline_s
        self._poll_interval_s = poll_interval_s

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self._http.request(method, self._base + path, **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def run_once(self) -> bool:
        """One synthetic order, end to end. False = the product is broken
        somewhere; the exception is logged with the failing step."""
        try:
            pair = await self._json(
                "POST",
                "/v1/auth/login",
                json={"email": "customer@demo.smartfood.dev", "password": "demo1234demo"},
            )
            auth = {"Authorization": f"Bearer {pair['access_token']}"}
            address = (await self._json("GET", "/v1/me/addresses", headers=auth))[0]["id"]
            restaurants = (await self._json("GET", "/v1/restaurants?city=springfield"))[
                "restaurants"
            ]
            rid = next(r["id"] for r in restaurants if r["name"] == "Biryani House")
            menu = await self._json("GET", f"/v1/menus/{rid}")
            item = next(
                i["id"]
                for c in menu["categories"]
                for i in c["items"]
                if i.get("available", True)
                and not any(g.get("min_select", 0) > 0 for g in i.get("modifier_groups", []))
            )
            placed = await self._json(
                "POST",
                "/v1/orders",
                headers={**auth, "Idempotency-Key": f"canary-{uuid.uuid4().hex}"},
                json={
                    "restaurant_id": rid,
                    "menu_version": menu["version"],
                    "address_id": address,
                    "card_token": "tok_ok",
                    "lines": [{"item_id": item, "qty": 1}],
                },
            )
            order_id = placed["order_id"]

            deadline = time.monotonic() + self._poll_deadline_s
            while True:
                status = (await self._json("GET", f"/v1/orders/{order_id}", headers=auth))["status"]
                if status == "CONFIRMED":
                    break
                if status in ("CANCELLED", "CANCELLING"):
                    log.error("canary order cancelled by the saga", order_id=order_id)
                    return False
                if time.monotonic() > deadline:
                    log.error("canary order never confirmed", order_id=order_id, status=status)
                    return False
                await asyncio.sleep(self._poll_interval_s)

            # Cleanup IS part of the check: the cancel path is product too.
            # Through _json (not a bare post) so a cancel that starts failing
            # FAILS THE CANARY. Discarding this response made the probe green
            # while the 180s accept-timer quietly compensated every synthetic
            # order — a real outage surfacing as SystemTimeoutCancels pages
            # instead of the canary that exists to name it.
            await self._json("POST", f"/v1/orders/{order_id}/cancel", headers=auth)
            log.info("canary order confirmed and cancelled", order_id=order_id)
            return True
        except Exception as exc:
            log.error("canary run failed", error=str(exc))
            return False

    async def tick(self) -> bool:
        ok = await self.run_once()
        RUNS.labels(outcome="ok" if ok else "fail").inc()
        if ok:
            LAST_SUCCESS.set(time.time())
        return ok

    async def run(self, interval_s: float) -> None:
        """Forever: one synthetic order per interval. Cancellation is the
        shutdown signal (the same contract as every loop in this repo)."""
        while True:
            await self.tick()
            await asyncio.sleep(interval_s)


async def _main() -> None:  # pragma: no cover — live wiring (compose runs it)
    setup_logging("canary")
    serve_metrics(int(os.environ.get("CANARY_METRICS_PORT", "9108")))
    async with httpx.AsyncClient(timeout=10.0) as http:
        canary = Canary(os.environ.get("GATEWAY_URL", "http://localhost:8080"), http)
        await canary.run(float(os.environ.get("CANARY_INTERVAL_SECONDS", "60")))


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_main())

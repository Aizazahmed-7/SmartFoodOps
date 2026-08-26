"""rider-sim — fake couriers for demos and drills (the mock-psp philosophy
applied to people: the interesting part of dispatch is how riders behave,
so the simulator's behavior is the configurable surface).

Each simulated rider is REST-FIRST: login → go online → poll /v1/rider/me
(the poll floor) → accept offers (per ACCEPT_RATE) → glide to the pickup →
tap pickup → glide to the dropoff → tap deliver. A WebSocket to the
rider-gateway streams the glide as 1 Hz GPS pings — the accelerator, run
best-effort: a sim rider whose socket dies keeps delivering on REST, which
is exactly the resilience claim the design makes for real riders.

Knobs (env): RIDERS (2), ACCEPT_RATE (1.0), SPEED_MPS (12 — a brisk
scooter), ONESHOT (unset; "1" = exit after the first completed delivery,
what `make demo` uses), GATEWAY_URL, WS_URL.

The decision core (`SimRider.decide`) is a pure function of the /me view —
tested; the transport loop is live wiring, exercised by the compose stack.
"""

import asyncio
import json
import os
import random
from dataclasses import dataclass, field

import httpx

from .motion import meters_between, step_toward

PASSWORD = "demo1234demo"
ARRIVE_M = 40.0  # close enough to tap — half a street on the toy map

# Scattered start corners inside the Springfield box (seed's CITY_BOXES).
START_SPOTS = [(39.7855, -89.6655), (39.8145, -89.6345), (39.7860, -89.6350)]


@dataclass
class Plan:
    """What the tick decided — the testable output shape."""

    move_to: tuple[float, float] | None = None
    accept: tuple[str, str] | None = None  # (offer_id, order_id)
    tap: tuple[str, str] | None = None  # (action, order_id)
    done: bool = False  # a delivery completed this tick (oneshot's cue)


@dataclass
class SimRider:
    email: str
    position: tuple[float, float]
    accept_rate: float = 1.0
    speed_mps: float = 12.0
    rng: random.Random = field(default_factory=random.Random)

    def decide(self, me: dict, *, dt_s: float) -> Plan:
        """One tick against the /me view. Priorities: finish the active
        job, else answer the live offer, else drift nowhere."""
        delivery = me.get("delivery")
        if delivery:
            target_key = "pickup" if delivery["state"] == "ASSIGNED" else "dropoff"
            target = (delivery[target_key]["lat"], delivery[target_key]["lon"])
            if meters_between(self.position, target) <= ARRIVE_M:
                action = "pickup" if target_key == "pickup" else "deliver"
                return Plan(tap=(action, delivery["order_id"]), done=(action == "deliver"))
            return Plan(
                move_to=step_toward(self.position, target, speed_mps=self.speed_mps, dt_s=dt_s)
            )
        offer = me.get("offer")
        if offer:
            if self.rng.random() < self.accept_rate:
                return Plan(accept=(offer["offer_id"], offer["order_id"]))
            return Plan()  # ghost it — the cascade's timeout is the answer
        return Plan()


async def run_rider(  # pragma: no cover — live wiring (the compose stack runs it)
    email: str, spot: tuple[float, float], *, gateway: str, ws_url: str, oneshot: bool
) -> None:
    rider = SimRider(
        email,
        spot,
        accept_rate=float(os.environ.get("ACCEPT_RATE", "1.0")),
        speed_mps=float(os.environ.get("SPEED_MPS", "12")),
    )
    async with httpx.AsyncClient(base_url=gateway, timeout=10.0) as http:
        await http.post("/v1/auth/register", json={"email": email, "password": PASSWORD})
        pair = (
            await http.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
        ).json()
        token = pair["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        online = await http.post(
            "/v1/rider/status",
            json={"online": True, "lat": rider.position[0], "lon": rider.position[1]},
            headers=auth,
        )
        online.raise_for_status()
        print(f"   {email} online at {rider.position}")

        stop = asyncio.Event()

        async def gps() -> None:
            """Best-effort socket: pings while it lives, reconnects when it
            dies, and NEVER takes the rider down with it."""
            import websockets
            from websockets.typing import Subprotocol

            while not stop.is_set():
                try:
                    protocols = [Subprotocol("bearer"), Subprotocol(token)]
                    async with websockets.connect(ws_url, subprotocols=protocols) as socket:
                        while not stop.is_set():
                            await socket.send(
                                json.dumps(
                                    {
                                        "type": "ping",
                                        "lat": rider.position[0],
                                        "lon": rider.position[1],
                                    }
                                )
                            )
                            await asyncio.sleep(1.0)
                except Exception:
                    await asyncio.sleep(2.0)  # gateway busy/booting — retry

        gps_task = asyncio.create_task(gps())
        try:
            while True:
                me = (await http.get("/v1/rider/me", headers=auth)).json()
                plan = rider.decide(me, dt_s=1.0)
                if plan.move_to is not None:
                    rider.position = plan.move_to
                if plan.accept is not None:
                    offer_id, order_id = plan.accept
                    accepted = await http.post(
                        f"/v1/rider/offers/{offer_id}/accept",
                        json={"order_id": order_id},
                        headers=auth,
                    )
                    print(f"   {email} accept {order_id} → {accepted.status_code}")
                if plan.tap is not None:
                    action, order_id = plan.tap
                    tapped = await http.post(
                        f"/v1/rider/deliveries/{order_id}/{action}", headers=auth
                    )
                    print(f"   {email} {action} {order_id} → {tapped.status_code}")
                if plan.done and oneshot:
                    print(f"   {email} delivered — oneshot done")
                    return
                await asyncio.sleep(1.0)
        finally:
            stop.set()
            gps_task.cancel()


async def _amain() -> None:  # pragma: no cover — entrypoint wiring
    gateway = os.environ.get("GATEWAY_URL", "http://localhost:8080")
    ws_url = os.environ.get("WS_URL", "ws://localhost:8080/ws/rider")
    riders = int(os.environ.get("RIDERS", "2"))
    oneshot = os.environ.get("ONESHOT", "") == "1"
    jobs = [
        run_rider(
            f"rider{i + 1}@demo.smartfood.dev",
            START_SPOTS[i % len(START_SPOTS)],
            gateway=gateway,
            ws_url=ws_url,
            oneshot=oneshot,
        )
        for i in range(riders)
    ]
    if oneshot:
        # First completed delivery wins; everyone else stands down.
        done, pending = await asyncio.wait(
            [asyncio.ensure_future(job) for job in jobs],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()  # surface a crash instead of a silent exit
    else:
        await asyncio.gather(*jobs)


def main() -> None:  # pragma: no cover
    asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover
    main()

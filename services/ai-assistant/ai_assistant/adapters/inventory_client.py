"""Kitchen congestion for the explanation engine (FR-85).

Mirrors `CatalogClient` down to the retry shape: system-role identity
headers (internal trust, ADR-0005) and two attempts rather than three,
because this sits on a customer's render path and not a money path.

Inventory's `active`/`capacity` is the only congestion signal the platform
has. It exists because reservations gate on it (FR-15), and until FR-85 it
was readable by nobody — so an explanation engine could not tell a
saturated kitchen from an idle one, and would have had to either stay
silent about the most common cause of a delay or invent a cause.
"""

import asyncio
from datetime import UTC, datetime

import httpx
from smartfood_auth import internal_headers
from smartfood_otel import get_logger

from ..domain.ports import KitchenLoad

log = get_logger("ai-assistant.inventory")


class InventoryClient:
    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        *,
        attempts: int = 2,
        retry_delay: float = 0.2,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http
        self._attempts = attempts
        self._retry_delay = retry_delay

    async def load(self, restaurant_id: str) -> KitchenLoad | None:
        """None on every failure, and on a 404.

        Deliberately one outcome for two situations. A 404 is a kitchen
        with no load row (it has never taken a reservation) and a timeout
        is an Inventory we could not reach — operationally different,
        identically empty to the resolver. In both cases nobody observed a
        congestion number, and the only honest thing an explanation can do
        is not mention congestion at all.

        Raising instead would be worse than useless: the customer asked why
        their order is late, and failing the whole answer because one
        contributing fact is unavailable turns a partial explanation into
        no explanation.
        """
        url = f"{self._base}/v1/internal/restaurants/{restaurant_id}/load"
        for attempt in range(self._attempts):
            if attempt:
                await asyncio.sleep(self._retry_delay * attempt)
            try:
                resp = await self._http.get(url, headers=internal_headers("ai-assistant"))
            except httpx.HTTPError:
                continue
            if resp.status_code == 200:
                return _parse(restaurant_id, resp.json())
            if resp.status_code == 404:
                return None  # no load row — unknown, not idle
            if resp.status_code < 500:
                log.warning(
                    "inventory refused a load read — explaining without congestion",
                    restaurant_id=restaurant_id,
                    status=resp.status_code,
                )
                return None
        log.warning("inventory unreachable — explaining without congestion")
        return None


def _parse(restaurant_id: str, body: object) -> KitchenLoad | None:
    """A malformed body is a missing fact, not a 500.

    Inventory is a first-party service and this should never fire — which
    is exactly why it must not be an exception. The failure mode being
    guarded is a contract drift that reaches production, and losing the
    congestion clause beats losing the customer's answer.
    """
    if not isinstance(body, dict):
        return None
    try:
        return KitchenLoad(
            restaurant_id=restaurant_id,
            active=int(body["active"]),
            capacity=int(body["capacity"]),
            as_of=_clock(body.get("as_of")),
        )
    except (KeyError, TypeError, ValueError):
        log.warning("inventory returned a load body we cannot read", restaurant_id=restaurant_id)
        return None


def _clock(raw: object) -> datetime:
    """Inventory's own clock, falling back to ours.

    Ours is a worse answer — it hides however long the call took — but a
    reading with no timestamp at all cannot be aged by the caller, and the
    caller is about to write the words "right now".
    """
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now(UTC)

import pytest
from fastapi.testclient import TestClient
from payment.config import Settings
from payment.domain.ports import GatewayResult
from payment.main import create_app


class FakeGateway:
    """Scripted PaymentGatewayPort: authorize pops from `script`
    (GatewayResult or an Exception to raise); lifecycle ops record calls
    and raise `fail_lifecycle` if set."""

    def __init__(self):
        self.script: list = []
        self.authorize_calls: list[tuple[str, int, str]] = []
        self.lifecycle_calls: list[tuple[str, str, str]] = []
        self.fail_lifecycle: Exception | None = None

    async def authorize(self, *, key, amount_cents, currency, card_token):
        self.authorize_calls.append((key, amount_cents, card_token))
        step = self.script.pop(0) if self.script else GatewayResult(True, "psp_abc")
        if isinstance(step, Exception):
            raise step
        return step

    async def capture(self, *, key, psp_ref):
        self._lifecycle("capture", key, psp_ref)

    async def void(self, *, key, psp_ref):
        self._lifecycle("void", key, psp_ref)

    async def refund(self, *, key, psp_ref):
        self._lifecycle("refund", key, psp_ref)

    def _lifecycle(self, op, key, psp_ref):
        self.lifecycle_calls.append((op, key, psp_ref))
        if self.fail_lifecycle is not None:
            raise self.fail_lifecycle


@pytest.fixture()
def gateway():
    return FakeGateway()


@pytest.fixture()
def client(gateway):
    settings = Settings(database_url="sqlite+aiosqlite://", create_all=True)
    app = create_app(settings, gateway=gateway)
    with TestClient(app) as c:
        yield c

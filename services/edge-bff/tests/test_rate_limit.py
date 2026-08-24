"""The edge rate limiter: budgets per class, identity-scoped buckets,
window rollover, the 429 contract, and — most important — fail-open.

The FakeRedis below implements exactly the Lua contract the limiter uses
(INCR + first-hit EXPIRE); its TTLs are driven by the same injected clock
as the limiter, so window rollover is a clock assignment, not a sleep."""

import httpx
from edge_bff.config import Settings
from edge_bff.limiter import RateLimiter
from edge_bff.main import create_app
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from smartfood_auth import JwksVerifier, TokenIssuer, generate_rsa_key, jwks
from smartfood_otel import REGISTRY

ISS = "http://identity.test"
AUD = "sfo-api"
KEY = generate_rsa_key()
ISSUER = TokenIssuer(KEY, issuer=ISS, audience=AUD)


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeRedis:
    """The limiter's Lua contract, in-memory: INCR, EXPIRE only on first
    hit, keys vanish when their deadline passes (per the shared clock)."""

    def __init__(self, clock: Clock, *, broken: bool = False):
        self._clock = clock
        self.broken = broken
        self._data: dict[str, tuple[int, float]] = {}  # key -> (count, expires_at)
        self.closed = False

    async def eval(self, script: str, numkeys: int, key: str, window: str):
        if self.broken:
            raise RedisError("connection refused")
        count, expires = self._data.get(key, (0, 0.0))
        if expires and expires <= self._clock.now:
            count = 0
        count += 1
        if count == 1:
            expires = self._clock.now + int(window)
        self._data[key] = (count, expires)
        return count

    async def aclose(self):
        self.closed = True


def upstream_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "identity.test" and request.url.path.endswith("jwks.json"):
        return httpx.Response(200, json=jwks([KEY]))
    return httpx.Response(200, json={"ok": True})


def make_client(fake: FakeRedis, clock: Clock) -> TestClient:
    settings = Settings(
        identity_base_url="http://identity.svc",
        catalog_base_url="http://catalog.svc",
        order_base_url="http://order.svc",
        notification_base_url="http://notification.svc",
        identity_jwks_url="http://identity.test/.well-known/jwks.json",
        token_issuer=ISS,
    )
    transport = httpx.MockTransport(upstream_handler)
    app = create_app(
        settings,
        http=httpx.AsyncClient(transport=transport),
        verifier=JwksVerifier(
            settings.identity_jwks_url,
            issuer=ISS,
            audience=AUD,
            http=httpx.AsyncClient(transport=transport),
        ),
        limiter=RateLimiter(
            fake,  # type: ignore[arg-type] — duck-typed on eval/aclose
            limits={"auth": 2, "read": 3, "write": 2},
            window_seconds=60,
            clock=clock,
        ),
    )
    return TestClient(app)


def bearer(sub="usr_1") -> dict:
    return {"Authorization": f"Bearer {ISSUER.issue(sub=sub, role='customer')}"}


def _denials(route_class: str) -> float:
    return REGISTRY.get_sample_value("rate_limited_total", {"route_class": route_class}) or 0.0


def test_reads_within_budget_then_429_with_the_full_header_contract():
    clock = Clock()
    with make_client(FakeRedis(clock), clock) as c:
        for _ in range(3):
            assert c.get("/v1/menus/rst_1").status_code == 200
        before = _denials("read")
        r = c.get("/v1/menus/rst_1")
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "RATE_LIMITED"
        assert r.headers["RateLimit-Limit"] == "3"
        assert r.headers["RateLimit-Remaining"] == "0"
        # reset points at the window boundary; Retry-After counts to it
        assert int(r.headers["RateLimit-Reset"]) % 60 == 0
        assert int(r.headers["Retry-After"]) >= 1
        assert _denials("read") == before + 1


def test_window_rollover_restores_the_budget():
    clock = Clock()
    with make_client(FakeRedis(clock), clock) as c:
        for _ in range(3):
            c.get("/v1/menus/rst_1")
        assert c.get("/v1/menus/rst_1").status_code == 429
        clock.now += 61  # the next fixed window
        assert c.get("/v1/menus/rst_1").status_code == 200


def test_authed_users_do_not_share_a_bucket():
    """The whole point of sub-scoping: user A exhausting their budget must
    not throttle user B behind the same address."""
    clock = Clock()
    with make_client(FakeRedis(clock), clock) as c:
        for _ in range(2):
            assert c.post("/v1/orders", json={}, headers=bearer("usr_a")).status_code == 200
        assert c.post("/v1/orders", json={}, headers=bearer("usr_a")).status_code == 429
        assert c.post("/v1/orders", json={}, headers=bearer("usr_b")).status_code == 200


def test_auth_class_is_the_tight_one():
    """Login is limited by IP at the auth budget — credential guessing is
    the attack this class exists for."""
    clock = Clock()
    with make_client(FakeRedis(clock), clock) as c:
        for _ in range(2):
            assert c.post("/v1/auth/login", json={}).status_code == 200
        r = c.post("/v1/auth/login", json={})
        assert r.status_code == 429


def test_first_forwarded_hop_scopes_anonymous_traffic():
    """Two NAT'd offices (different XFF) get separate anonymous buckets."""
    clock = Clock()
    with make_client(FakeRedis(clock), clock) as c:
        for _ in range(3):
            c.get("/v1/menus/rst_1", headers={"X-Forwarded-For": "10.0.0.1, 172.16.0.9"})
        assert c.get("/v1/menus/rst_1", headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 429
        assert c.get("/v1/menus/rst_1", headers={"X-Forwarded-For": "10.0.0.2"}).status_code == 200


def test_redis_down_fails_open():
    """The contract that must never regress: a limiter outage is NOT an
    edge outage. Every request flows; the failure is counted, not thrown."""
    clock = Clock()
    errors_before = REGISTRY.get_sample_value("rate_limit_errors_total") or 0.0
    with make_client(FakeRedis(clock, broken=True), clock) as c:
        for _ in range(10):  # far past every budget
            assert c.get("/v1/menus/rst_1").status_code == 200
    assert (REGISTRY.get_sample_value("rate_limit_errors_total") or 0.0) == errors_before + 10


def test_redis_url_builds_a_real_client_and_still_fails_open():
    """The un-injected path create_app takes in production: a redis_url is
    given, a real client is built (no I/O at construction), and — because
    port 1 refuses instantly — every check fails OPEN. Also exercises the
    lifespan closing a real client."""
    settings = Settings(
        identity_base_url="http://identity.svc",
        catalog_base_url="http://catalog.svc",
        order_base_url="http://order.svc",
        notification_base_url="http://notification.svc",
        identity_jwks_url="http://identity.test/.well-known/jwks.json",
        token_issuer=ISS,
        redis_url="redis://127.0.0.1:1/0",
    )
    transport = httpx.MockTransport(upstream_handler)
    app = create_app(
        settings,
        http=httpx.AsyncClient(transport=transport),
        verifier=JwksVerifier(
            settings.identity_jwks_url,
            issuer=ISS,
            audience=AUD,
            http=httpx.AsyncClient(transport=transport),
        ),
    )
    with TestClient(app) as c:
        assert c.get("/v1/menus/rst_1").status_code == 200


async def test_disarmed_limiter_answers_none_and_close_is_safe():
    limiter = RateLimiter(None, limits={"read": 1})
    assert await limiter.check("read", "ip:1.2.3.4") is None
    await limiter.aclose()  # no client — must not raise


async def test_unknown_route_class_is_unlimited():
    """A class with no configured budget answers None rather than KeyError —
    adding a new class to routing before config must not 500 the edge."""
    clock = Clock()
    limiter = RateLimiter(
        FakeRedis(clock),  # type: ignore[arg-type]
        limits={"read": 1},
        clock=clock,
    )
    assert await limiter.check("mystery", "ip:1.2.3.4") is None


async def test_aclose_closes_the_client():
    clock = Clock()
    fake = FakeRedis(clock)
    limiter = RateLimiter(fake, limits={}, clock=clock)  # type: ignore[arg-type]
    await limiter.aclose()
    assert fake.closed

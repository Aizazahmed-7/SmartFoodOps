import httpx
import jwt
import pytest
from smartfood_auth import JwksVerifier, TokenIssuer, generate_rsa_key, jwks

ISS = "https://identity.test"
AUD = "sfo-api"


def make_verifier(jwks_docs: list[dict]) -> JwksVerifier:
    """Verifier whose HTTP client serves each jwks doc in sequence (last one repeats)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        doc = jwks_docs[min(calls["n"], len(jwks_docs) - 1)]
        calls["n"] += 1
        return httpx.Response(200, json=doc)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JwksVerifier("https://identity.test/jwks", issuer=ISS, audience=AUD, http=http)


async def test_round_trip():
    key = generate_rsa_key()
    token = TokenIssuer(key, issuer=ISS, audience=AUD).issue(
        sub="u1", role="customer"
    )
    claims = await make_verifier([jwks([key])]).verify(token)
    assert claims["sub"] == "u1"
    assert claims["role"] == "customer"


async def test_scoping_claims_survive():
    key = generate_rsa_key()
    token = TokenIssuer(key, issuer=ISS, audience=AUD).issue(
        sub="u2", role="restaurant_admin", restaurant_id="rest_9"
    )
    claims = await make_verifier([jwks([key])]).verify(token)
    assert claims["restaurant_id"] == "rest_9"


async def test_expired_token_rejected():
    key = generate_rsa_key()
    token = TokenIssuer(key, issuer=ISS, audience=AUD, ttl_seconds=-10).issue(
        sub="u1", role="customer"
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        await make_verifier([jwks([key])]).verify(token)


async def test_wrong_audience_rejected():
    key = generate_rsa_key()
    token = TokenIssuer(key, issuer=ISS, audience="someone-else").issue(
        sub="u1", role="customer"
    )
    with pytest.raises(jwt.InvalidAudienceError):
        await make_verifier([jwks([key])]).verify(token)


async def test_unknown_kid_triggers_refetch():
    """Key rotation: first fetch serves old key; token signed by new key forces a refetch."""
    old, new = generate_rsa_key(), generate_rsa_key()
    verifier = make_verifier([jwks([old]), jwks([old, new])])

    old_token = TokenIssuer(old, issuer=ISS, audience=AUD).issue(sub="u1", role="customer")
    assert (await verifier.verify(old_token))["sub"] == "u1"  # primes cache with old only

    new_token = TokenIssuer(new, issuer=ISS, audience=AUD).issue(sub="u2", role="customer")
    assert (await verifier.verify(new_token))["sub"] == "u2"  # unknown kid → refetch → ok


async def test_forged_token_rejected():
    """A token signed by a key NOT in the JWKS must fail signature verification."""
    trusted, attacker = generate_rsa_key(), generate_rsa_key()
    forged = TokenIssuer(attacker, issuer=ISS, audience=AUD).issue(sub="u1", role="system_admin")
    with pytest.raises(jwt.InvalidTokenError):
        await make_verifier([jwks([trusted])]).verify(forged)


async def test_alg_none_rejected():
    key = generate_rsa_key()
    evil = jwt.encode({"sub": "u1", "iss": ISS, "aud": AUD}, key=None, algorithm="none")
    with pytest.raises(jwt.InvalidTokenError):
        await make_verifier([jwks([key])]).verify(evil)

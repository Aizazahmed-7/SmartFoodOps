from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from smartfood_api import ApiError, StrictModel, install_error_handlers
from smartfood_otel import RequestContextMiddleware

app = FastAPI()
app.add_middleware(RequestContextMiddleware)
install_error_handlers(app)


class Body(StrictModel):
    qty: int


@app.post("/echo")
async def echo(body: Body) -> dict:
    return {"qty": body.qty}


@app.get("/boom-api")
async def boom_api():
    raise ApiError("NOT_FOUND", "no such thing", 404)


@app.get("/boom-http")
async def boom_http():
    raise HTTPException(status_code=403, detail="nope")


@app.get("/boom-crash")
async def boom_crash():
    raise RuntimeError("secret internals")


client = TestClient(app, raise_server_exceptions=False)


def test_api_error_renders_envelope_with_request_id():
    r = client.get("/boom-api")
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "NOT_FOUND"
    assert err["message"] == "no such thing"
    assert err["request_id"] == r.headers["x-request-id"]


def test_validation_error_has_details():
    r = client.post("/echo", json={"qty": "not-a-number"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_FAILED"
    assert err["details"][0]["field"] == "qty"


def test_strict_model_rejects_unknown_fields():
    r = client.post("/echo", json={"qty": 1, "surprise": 1})
    assert r.status_code == 422


def test_http_exception_mapped():
    r = client.get("/boom-http")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN_ROLE"


def test_unhandled_never_leaks():
    r = client.get("/boom-crash")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in str(body)

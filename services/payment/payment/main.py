"""payment — skeleton. Only /healthz until we build this service together."""

from fastapi import FastAPI

app = FastAPI(title="payment")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "payment"}

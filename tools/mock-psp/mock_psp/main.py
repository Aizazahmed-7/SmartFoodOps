"""Mock PSP — skeleton. Authorize/capture/void/refund land when we build Payment."""

from fastapi import FastAPI

app = FastAPI(title="mock-psp")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}

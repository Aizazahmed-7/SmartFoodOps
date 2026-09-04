"""OpenAI embeddings behind EmbeddingPort.

`dimensions` is sent explicitly because these embeddings are Matryoshka-
truncatable: asking for 512 of 1536 is a ~3x storage cut across the whole
corpus for a small, measurable relevance cost. The number is a Settings
knob and it is written to every row, because vectors produced at different
dimensions or by a different model are not comparable — changing either is
a `model_version` bump and a rolling reindex (PRD FR-61), never an in-place
edit.

Results are re-sorted by the response's `index` field rather than trusted
to arrive in order. The API does return them in order; making the alignment
explicit costs one sort and removes a silent, catastrophic failure mode
where every chunk gets its neighbour's vector.
"""

from collections.abc import Sequence
from typing import Any

import httpx

from ..domain.ports import EmbeddingUnavailable
from ._http import HttpCallFailed, RetryPolicy, post_json


class OpenAiEmbeddings:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        http: httpx.AsyncClient,
        model: str,
        dimensions: int,
        timeout_s: float = 30.0,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._http = http
        self._model = model
        self._dimensions = dimensions
        self._timeout_s = timeout_s
        self._retry = retry or RetryPolicy()

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            payload = await post_json(
                self._http,
                f"{self._base}/v1/embeddings",
                headers={
                    "authorization": f"Bearer {self._key}",
                    "content-type": "application/json",
                },
                body={
                    "model": self._model,
                    "input": list(texts),
                    "dimensions": self._dimensions,
                },
                timeout_s=self._timeout_s,
                retry=self._retry,
            )
        except HttpCallFailed as exc:
            raise EmbeddingUnavailable(str(exc)) from exc
        rows: list[dict[str, Any]] = [
            row for row in (payload.get("data") or []) if isinstance(row, dict)
        ]
        if len(rows) != len(texts):
            # Loud, because the alternative is a corpus of misaligned
            # vectors that no query error would ever explain.
            raise EmbeddingUnavailable(
                f"embedding count mismatch: asked {len(texts)}, got {len(rows)}"
            )
        rows.sort(key=lambda row: int(row.get("index", 0)))
        return [[float(value) for value in row.get("embedding") or []] for row in rows]

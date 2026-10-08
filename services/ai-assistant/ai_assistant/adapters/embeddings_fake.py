"""A deterministic local embedder, for when no provider key is set.

The disarm convention of ADR-0030 §7 says an empty key REMOVES a provider
rather than producing 401s at request time. For generation that means a
clean 503 and a degraded panel. For EMBEDDINGS the same rule would mean the
knowledge pipeline simply does not run, and with it every B1 demo — which
would make `make up-ai` useless without a credential and put the whole
milestone's live proof behind a vendor account.

So embeddings get a fallback rather than a refusal, and the unit suite needs
no key and no network (NFR-33).

**What this is not.** It is feature hashing — each token lands in a
dimension by its own hash — so texts that share WORDS come out close
together and texts that share MEANING do not. "something light" will not
find a salad here. That is fine for what B1 has to prove (a menu edit
reaches the index; a replay embeds nothing; deletions reconcile), all of
which are about plumbing and identity, not relevance. Retrieval QUALITY is
B2's milestone, measured on the golden set against a real provider — and
the eval suite is what would catch someone shipping this to production,
along with the loud `provider="fake"` on every metric.
"""

import hashlib
import math
import re
from collections.abc import Sequence

_TOKEN = re.compile(r"[a-z0-9]+")

FAKE_MODEL = "fake-hashing-v1"
"""Part of `model_version`, so it lands in every row and every query
predicate — which is the point. A corpus embedded by the fake is visibly,
queryably fake, and switching to a real provider is a model-version change
that triggers the ordinary rolling reindex rather than a silent mixture."""


class FakeEmbeddings:
    """Implements `EmbeddingPort`. Same text always yields the same vector,
    in this process and the next one — `content_hash` is only meaningful if
    embedding is a pure function of the text, and a `random` seeded from
    Python's own `hash()` would break that across restarts."""

    def __init__(self, *, dimensions: int, model: str = FAKE_MODEL) -> None:
        self._dimensions = dimensions
        self._model = model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            # The 5th byte's low bit picks the sign, the standard hashing-trick
            # trick: without it, every token collision adds, and unrelated
            # texts drift toward each other as the corpus grows.
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if not norm:
            # No tokens at all — an unnamed item, or punctuation only. A zero
            # vector has no direction, and `<=>` against it is undefined in
            # pgvector; a fixed unit vector keeps the column's NOT NULL
            # honest and parks every such chunk in the same harmless corner.
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]

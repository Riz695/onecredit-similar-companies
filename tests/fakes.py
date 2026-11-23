"""Test doubles that stand in for the embedding model."""

import hashlib
import math

DIMENSIONS = 384


def unit_vector(*leading: float) -> list[float]:
    """A 384-dimensional unit vector whose first components are `leading` (before normalising)."""
    values = [*leading, *([0.0] * (DIMENSIONS - len(leading)))]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class FakeEmbedder:
    """Deterministic bag-of-words embedder: texts that share words get similar vectors.

    Satisfies the `Embedder` protocol without downloading a model, so tests stay fast and offline.
    """

    dimensions = DIMENSIONS

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        values = [0.0] * DIMENSIONS
        for word in text.lower().split():
            bucket = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % DIMENSIONS
            values[bucket] += 1.0
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]

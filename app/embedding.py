"""Turning a business description into a vector."""

import os
from typing import Protocol

from fastembed import TextEmbedding


class Embedder(Protocol):
    """Anything that maps texts to unit-length 384-dimensional vectors (the size of `companies.embedding`).

    Tests use a fake; production uses `FastEmbedder`.
    """

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    """An open-source sentence-embedding model run on the CPU with ONNX Runtime (via fastembed).

    The default, BAAI/bge-small-en-v1.5, returns 384-dimensional L2-normalised vectors. The weights
    (~65 MB) are downloaded once on first use and cached.
    """

    def __init__(self, model_name: str) -> None:
        # ONNX Runtime's default (one thread per logical core) was 4x slower than 4 threads on a
        # 12-thread laptop for one short text at a time: the threads mostly wait on each other.
        self._model = TextEmbedding(model_name, threads=min(4, os.cpu_count() or 1))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(texts)]

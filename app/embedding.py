"""Turning a business description into a vector."""

from typing import Protocol

from fastembed import TextEmbedding


class Embedder(Protocol):
    """Anything that maps texts to unit-length vectors. Tests use a fake; production uses `FastEmbedder`."""

    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    """An open-source sentence-embedding model run on the CPU with ONNX Runtime (via fastembed).

    The default, BAAI/bge-small-en-v1.5, returns 384-dimensional L2-normalised vectors. The weights
    (~65 MB) are downloaded once on first use and cached.
    """

    dimensions = 384

    def __init__(self, model_name: str) -> None:
        self._model = TextEmbedding(model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(texts)]

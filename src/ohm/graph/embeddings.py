"""Pluggable embedding backend system (OHM-9zk7).

Provides a unified interface for generating embeddings from multiple providers:
- OllamaBackend: Local Ollama instance (default)
- OpenAIBackend: OpenAI API embeddings
- NullBackend: No-op backend returning zero vectors (for airgapped/disabled embeddings)

Usage:
    from ohm.graph.embeddings import OllamaBackend, OpenAIBackend, NullBackend

    # Default: Ollama if available, NullBackend otherwise
    backend = make_embedding_backend()

    # Explicit backend selection
    backend = OllamaBackend(model="nomic-embed-text", ollama_url="http://localhost:11434")
    backend = OpenAIBackend(api_key=os.environ["OPENAI_API_KEY"], model="text-embedding-3-small")
    backend = NullBackend(dimensions=768)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


class EmbeddingBackend(ABC):
    """Protocol for embedding backends.

    Implement this interface to add new embedding providers.
    All backends must be thread-safe for concurrent use.
    """

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Return the embedding vector dimensions."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a list of texts.

        Args:
            texts: List of text strings to embed.

        Returns:
            List of embedding vectors (list of floats), one per input text.
            Must return exactly len(texts) embeddings.
            Return zero vectors on failure (do not raise).
        """

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the backend is available/healthy."""


def make_embedding_backend(
    backend: str | None = None,
    ollama_url: str = "http://localhost:11434",
    ollama_model: str = "nomic-embed-text",
    openai_api_key: str | None = None,
    openai_model: str = "text-embedding-3-small",
    null_dimensions: int = 768,
) -> EmbeddingBackend:
    """Factory function to create an embedding backend.

    Args:
        backend: Backend name - "ollama", "openai", or "null". If None, auto-detects.
        ollama_url: URL for Ollama API (used by OllamaBackend).
        ollama_model: Model name for Ollama (default: nomic-embed-text).
        openai_api_key: API key for OpenAI (used by OpenAIBackend).
        openai_model: Model name for OpenAI (default: text-embedding-3-small).
        null_dimensions: Dimensions for NullBackend (default: 768).

    Returns:
        An EmbeddingBackend instance.

    Auto-detection logic (when backend=None):
        1. Try OllamaBackend first (check is_available())
        2. Fall back to NullBackend if Ollama is not available
    """
    if backend is None:
        ollama = OllamaBackend(model=ollama_model, ollama_url=ollama_url)
        if ollama.is_available():
            return ollama
        return NullBackend(dimensions=null_dimensions)

    if backend == "ollama":
        return OllamaBackend(model=ollama_model, ollama_url=ollama_url)
    elif backend == "openai":
        if not openai_api_key:
            raise ValueError("OpenAI API key required for openai backend")
        return OpenAIBackend(api_key=openai_api_key, model=openai_model)
    elif backend == "null":
        return NullBackend(dimensions=null_dimensions)
    else:
        raise ValueError(f"Unknown embedding backend: {backend}")


class OllamaBackend(EmbeddingBackend):
    """Embedding backend using a local Ollama instance."""

    def __init__(
        self,
        model: str = "nomic-embed-text",
        ollama_url: str = "http://localhost:11434",
    ) -> None:
        self._model = model
        self._url = ollama_url

    @property
    def dimensions(self) -> int:
        return 768

    def is_available(self) -> bool:
        try:
            import urllib.request

            req = urllib.request.Request(
                f"{self._url.rstrip('/')}/api/tags",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        try:
            import json as _json
            import urllib.request

            url = f"{self._url.rstrip('/')}/api/embed"
            payload = _json.dumps({"model": self._model, "input": texts}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
                embeddings = data.get("embeddings", [])
                if isinstance(embeddings, list) and len(embeddings) == len(texts):
                    return [e if isinstance(e, list) else [] for e in embeddings]
        except Exception:
            pass

        return [[0.0] * self.dimensions for _ in texts]


class OpenAIBackend(EmbeddingBackend):
    """Embedding backend using the OpenAI API."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def is_available(self) -> bool:
        try:
            import urllib.request

            req = urllib.request.Request(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception:
            return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        try:
            import json as _json
            import urllib.request

            url = "https://api.openai.com/v1/embeddings"
            payload = _json.dumps(
                {
                    "model": self._model,
                    "input": texts,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
                embeddings = data.get("data", [])
                if len(embeddings) == len(texts):
                    return [e.get("embedding", []) for e in embeddings]
        except Exception:
            pass

        return [[0.0] * self._dimensions for _ in texts]


class NullBackend(EmbeddingBackend):
    """No-op embedding backend returning zero vectors.

    Use this when embeddings are disabled or the embedding service is unavailable.
    Allows the graph to function without embeddings while avoiding errors.
    """

    def __init__(self, dimensions: int = 768) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def is_available(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._dimensions for _ in texts]

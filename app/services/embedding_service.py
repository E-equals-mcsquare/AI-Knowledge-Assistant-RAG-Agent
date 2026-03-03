"""
app/services/embedding_service.py

Wraps the OpenAI Embeddings API to generate dense vector representations
of text strings.

Design notes:
- Automatic batching: OpenAI recommends batches ≤ 2048 items; we use 100
  as a safe default that avoids per-request token-limit edge cases.
- Newlines replaced with spaces: recommended by OpenAI for best quality.
- Raises OpenAIError on API failures (caller decides how to handle).

Future:
- Add a local sentence-transformers fallback for offline / air-gapped envs
- Implement an async embed_texts_async() for non-blocking ingestion pipelines
- Add token counting (tiktoken) to warn when individual texts are too long
"""

from typing import List

from openai import OpenAI, OpenAIError

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_EMBEDDING_BATCH_SIZE = 100  # max texts per single API call


class EmbeddingService:
    """
    Generates text embeddings using the OpenAI Embeddings API.

    Singleton-safe: holds only a stateless OpenAI client — safe to share
    across concurrent requests.
    """

    def __init__(self, settings: Settings) -> None:
        self._client = OpenAI(api_key=settings.OPENAI_API_KEY)
        self._model = settings.OPENAI_EMBEDDING_MODEL

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for a list of text strings.

        Processes in batches to stay within API limits.
        Returns one float vector per input text, in the same order.

        Args:
            texts: List of raw text strings to embed.

        Returns:
            List of embedding vectors (List[List[float]]).

        Raises:
            OpenAIError: On API-level failures.
        """
        if not texts:
            return []

        # Pre-process: collapse newlines, strip whitespace, drop empties
        cleaned = [t.replace("\n", " ").strip() for t in texts]
        cleaned = [t for t in cleaned if t]
        if not cleaned:
            return []

        all_embeddings: List[List[float]] = []

        for batch_start in range(0, len(cleaned), _EMBEDDING_BATCH_SIZE):
            batch = cleaned[batch_start : batch_start + _EMBEDDING_BATCH_SIZE]
            batch_num = batch_start // _EMBEDDING_BATCH_SIZE + 1
            total_batches = (len(cleaned) - 1) // _EMBEDDING_BATCH_SIZE + 1

            try:
                logger.debug(
                    f"Embedding batch {batch_num}/{total_batches} "
                    f"({len(batch)} texts) via {self._model}"
                )
                response = self._client.embeddings.create(
                    model=self._model,
                    input=batch,
                    encoding_format="float",
                )
                batch_embeddings = [item.embedding for item in response.data]
                all_embeddings.extend(batch_embeddings)

            except OpenAIError as exc:
                logger.error(
                    f"OpenAI embedding error at batch {batch_num}/{total_batches}: {exc}"
                )
                raise

        dim = len(all_embeddings[0]) if all_embeddings else 0
        logger.info(
            f"Generated {len(all_embeddings)} embeddings "
            f"(model={self._model}, dim={dim})"
        )
        return all_embeddings

    def embed_query(self, query: str) -> List[float]:
        """
        Generate a single embedding for a query string.

        Convenience wrapper around embed_texts() for the retrieval path.

        Args:
            query: Natural language question.

        Returns:
            A single embedding vector.

        Raises:
            ValueError:   If the query is empty after cleaning.
            OpenAIError:  On API-level failures.
        """
        if not query.strip():
            raise ValueError("Query string must not be empty.")

        vectors = self.embed_texts([query])
        if not vectors:
            raise ValueError(
                "Embedding service returned no results for the query."
            )
        return vectors[0]

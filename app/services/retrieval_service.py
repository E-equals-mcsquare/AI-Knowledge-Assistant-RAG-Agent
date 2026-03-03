"""
app/services/retrieval_service.py

Orchestrates the retrieval pipeline:
  1. Embed the incoming query
  2. Search the vector store for top-k candidates
  3. Filter candidates by similarity threshold
  4. Return ranked, relevant chunks

The similarity threshold is the primary hallucination-prevention gate:
any chunk whose cosine score falls below the threshold is discarded
before the context is passed to the LLM.

Design notes:
- Pure orchestration — holds no state beyond injected dependencies.
- Logging at INFO captures retrieval statistics per request; DEBUG shows
  per-chunk scores for troubleshooting threshold calibration.

Future:
- Add re-ranking (Cohere Rerank / cross-encoder) as an optional second pass
- Add MMR (Maximum Marginal Relevance) to reduce redundant context chunks
- Expose an async retrieve_async() for non-blocking ingestion workflows
"""

from dataclasses import dataclass
from typing import List, Optional

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.embedding_service import EmbeddingService
from app.services.vector_store import ChunkMetadata, VectorStoreService

logger = get_logger(__name__)


@dataclass
class RetrievedChunk:
    """A candidate chunk that passed the similarity threshold."""

    metadata: ChunkMetadata
    score: float  # cosine similarity ∈ [0, 1] after L2 normalisation


class RetrievalService:
    """
    Retrieves the most relevant document chunks for a given query.

    Depends on EmbeddingService and VectorStoreService — both injected,
    allowing easy swapping or mocking in tests.
    """

    def __init__(
        self,
        embedding_service: EmbeddingService,
        vector_store: VectorStoreService,
        settings: Settings,
    ) -> None:
        self._embedding_service = embedding_service
        self._vector_store = vector_store
        self._similarity_threshold = settings.SIMILARITY_THRESHOLD
        self._default_top_k = settings.TOP_K

    def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
    ) -> List[RetrievedChunk]:
        """
        Find the most relevant chunks for a natural language query.

        Args:
            query:  The user's question.
            top_k:  Number of chunks to retrieve. Falls back to the
                    TOP_K setting when not provided.

        Returns:
            List of RetrievedChunk objects whose cosine similarity score
            is ≥ SIMILARITY_THRESHOLD, ordered by score descending.
            Returns an empty list when no chunks meet the threshold —
            the caller treats this as "information not found."
        """
        effective_k = top_k if top_k is not None else self._default_top_k

        logger.info(
            f"Retrieval | query='{query[:80]}{'...' if len(query) > 80 else ''}' | "
            f"top_k={effective_k} | threshold={self._similarity_threshold}"
        )

        # Step 1: Embed the query
        query_embedding = self._embedding_service.embed_query(query)

        # Step 2: Nearest-neighbour search
        raw_results = self._vector_store.search(
            query_embedding=query_embedding,
            top_k=effective_k,
        )

        # Step 3: Threshold filtering
        passed: List[RetrievedChunk] = []
        for metadata, score in raw_results:
            if score >= self._similarity_threshold:
                passed.append(RetrievedChunk(metadata=metadata, score=score))
                logger.debug(
                    f"  PASS  chunk_id={metadata.chunk_id:>4} | "
                    f"score={score:.4f} | src={metadata.source_filename}"
                )
            else:
                logger.debug(
                    f"  SKIP  chunk_id={metadata.chunk_id:>4} | "
                    f"score={score:.4f} < threshold={self._similarity_threshold} | "
                    f"src={metadata.source_filename}"
                )

        logger.info(
            f"Retrieval complete: {len(passed)}/{len(raw_results)} chunks "
            f"passed threshold={self._similarity_threshold}"
        )
        return passed

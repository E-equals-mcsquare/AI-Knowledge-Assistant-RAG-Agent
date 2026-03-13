"""
app/services/vector_store_pinecone.py

Pinecone-backed vector store that implements the same public interface as
VectorStoreService (FAISS), making it a drop-in replacement.

Public interface (identical to vector_store.py):
    add_chunks(chunks, embeddings) -> None
    search(query_embedding, top_k)  -> List[Tuple[ChunkMetadata, float]]
    total_vectors                   -> int (property)

Key differences from the FAISS implementation:
- No local disk persistence — Pinecone is fully managed.
- Metadata (text, source, chunk_id, document_id) stored directly in Pinecone
  as vector metadata fields; no separate JSON sidecar needed.
- Vector IDs are semantic strings: "{document_id}__{chunk_id}" instead of
  sequential integers. Collision-safe and human-readable in the Pinecone console.
- No L2 normalisation needed — Pinecone's cosine metric handles it internally.
- Index creation is idempotent: if the named index already exists, it is reused.

Pinecone SDK used: pinecone >= 5.0.0  (from pinecone import Pinecone, ServerlessSpec)

Future:
- Add namespace support for multi-tenant isolation (one namespace per team/project)
- Add metadata filtering in search() for document-scoped retrieval
- Add delete_document(document_id) using Pinecone's delete-by-metadata
"""

from typing import Any, List, Tuple

from pinecone import Pinecone, ServerlessSpec

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.document_processor import TextChunk
from app.services.vector_store import ChunkMetadata

logger = get_logger(__name__)

# Pinecone metadata field names — kept as constants to avoid typos
_FIELD_DOCUMENT_ID    = "document_id"
_FIELD_CHUNK_ID       = "chunk_id"
_FIELD_SOURCE         = "source_filename"
_FIELD_TEXT           = "text"


def _make_vector_id(document_id: str, chunk_id: int) -> str:
    """
    Build a stable, human-readable Pinecone vector ID.

    Format: "{document_id}__{chunk_id}"
    The double-underscore separator is unlikely to appear in a UUID.
    """
    return f"{document_id}__{chunk_id}"


class PineconeVectorStoreService:
    """
    Pinecone-backed vector store.

    Drop-in replacement for VectorStoreService. Swap via VECTOR_STORE_BACKEND=pinecone.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.PINECONE_API_KEY:
            raise ValueError(
                "PINECONE_API_KEY is required when VECTOR_STORE_BACKEND=pinecone. "
                "Set it in your .env file."
            )

        self._similarity_threshold = settings.SIMILARITY_THRESHOLD
        self._index_name = settings.PINECONE_INDEX_NAME
        self._dim = settings.PINECONE_EMBEDDING_DIM

        self._pc = Pinecone(api_key=settings.PINECONE_API_KEY)
        self._index = self._get_or_create_index(
            cloud=settings.PINECONE_CLOUD,
            region=settings.PINECONE_REGION,
        )

        stats = self._index.describe_index_stats()
        logger.info(
            f"Pinecone index '{self._index_name}' ready | "
            f"vectors={stats.total_vector_count} | "
            f"dim={self._dim}"
        )

    # ------------------------------------------------------------------
    # Public interface (matches VectorStoreService exactly)
    # ------------------------------------------------------------------

    def add_chunks(
        self,
        chunks: List[TextChunk],
        embeddings: List[List[float]],
    ) -> None:
        """
        Upsert text chunks and their embeddings into Pinecone.

        Metadata is stored inline with each vector so no external sidecar
        is needed. Uses upsert (not insert) so re-ingesting the same document
        is safe and idempotent.

        Args:
            chunks:     TextChunk objects carrying metadata.
            embeddings: One float vector per chunk (same order).
        """
        if not chunks:
            return
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"Chunk/embedding count mismatch: "
                f"{len(chunks)} chunks vs {len(embeddings)} embeddings."
            )

        # Build vectors as (id, values, metadata) tuples — matches the
        # VectorTupleWithMetadata type that Pinecone's upsert() expects,
        # avoiding dict[str, Unknown] type errors with Pylance.
        vectors: List[tuple] = [
            (
                _make_vector_id(chunk.document_id, chunk.chunk_id),
                embedding,
                {
                    _FIELD_DOCUMENT_ID: chunk.document_id,
                    _FIELD_CHUNK_ID:    chunk.chunk_id,
                    _FIELD_SOURCE:      chunk.source_filename,
                    _FIELD_TEXT:        chunk.text,
                },
            )
            for chunk, embedding in zip(chunks, embeddings)
        ]

        # Pinecone recommends batches of ≤ 100 vectors per upsert call
        _UPSERT_BATCH = 100
        for i in range(0, len(vectors), _UPSERT_BATCH):
            batch = vectors[i : i + _UPSERT_BATCH]
            self._index.upsert(vectors=batch)
            logger.debug(f"Upserted batch {i // _UPSERT_BATCH + 1} ({len(batch)} vectors)")

        logger.info(
            f"Upserted {len(chunks)} chunks to Pinecone index '{self._index_name}'"
        )

    def search(
        self,
        query_embedding: List[float],
        top_k: int,
    ) -> List[Tuple[ChunkMetadata, float]]:
        """
        Query Pinecone for the top_k most similar chunks.

        Args:
            query_embedding: Single query vector.
            top_k:           Maximum number of results to return.

        Returns:
            List of (ChunkMetadata, cosine_score) tuples sorted by score
            descending. Returns [] when the index is empty.
        """
        stats = self._index.describe_index_stats()
        if stats.total_vector_count == 0:
            logger.warning("Pinecone index is empty — no results to return.")
            return []

        # Cast to Any: Pylance infers ApplyResult[Unknown] for query() due to
        # incomplete stubs in the Pinecone SDK. Runtime type is QueryResponse.
        response: Any = self._index.query(
            vector=query_embedding,
            top_k=min(top_k, stats.total_vector_count),
            include_metadata=True,
        )

        results: List[Tuple[ChunkMetadata, float]] = []
        for match in response.matches:
            meta = match.metadata or {}
            chunk_meta = ChunkMetadata(
                document_id=meta.get(_FIELD_DOCUMENT_ID, ""),
                chunk_id=int(meta.get(_FIELD_CHUNK_ID, 0)),
                source_filename=meta.get(_FIELD_SOURCE, ""),
                text=meta.get(_FIELD_TEXT, ""),
            )
            results.append((chunk_meta, float(match.score)))

        logger.debug(
            f"Pinecone query returned {len(results)} candidates for top_k={top_k}"
        )
        return results

    @property
    def total_vectors(self) -> int:
        """Return the current number of vectors in the Pinecone index."""
        try:
            return self._index.describe_index_stats().total_vector_count
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _get_or_create_index(
        self,
        cloud: str,
        region: str,
    ):
        """
        Connect to an existing Pinecone index or create it if it doesn't exist.

        Index creation is idempotent — safe to call on every startup.
        Uses Serverless spec (pay-per-use, no pod provisioning needed).
        """
        existing = [idx.name for idx in self._pc.list_indexes()]

        if self._index_name not in existing:
            logger.info(
                f"Creating Pinecone index '{self._index_name}' "
                f"(dim={self._dim}, metric=cosine, "
                f"cloud={cloud}, region={region})"
            )
            self._pc.create_index(
                name=self._index_name,
                dimension=self._dim,
                metric="cosine",
                spec=ServerlessSpec(cloud=cloud, region=region),
            )
            logger.info(f"Pinecone index '{self._index_name}' created successfully.")
        else:
            logger.info(f"Pinecone index '{self._index_name}' already exists — reusing.")

        return self._pc.Index(self._index_name)

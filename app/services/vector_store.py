"""
app/services/vector_store.py

Manages the FAISS vector index and its associated chunk metadata.

Architecture decisions:
- IndexFlatIP (Inner Product) + L2 normalisation = exact cosine similarity
  search. No approximation errors, suitable for corpora up to ~1M vectors.
- Metadata (text, source, chunk_id) stored separately as JSON. FAISS stores
  only float32 vectors; FAISS integer IDs map 1-to-1 to metadata entries.
- Threading: a reentrant lock guards all mutating operations (add + save).
  Concurrent reads (search) are safe once FAISS has no pending write.

Future migration paths:
- Replace JSON metadata store with a lightweight SQLite (via sqlmodel) for
  richer filtering (by document_id, date range, etc.)
- Swap IndexFlatIP for IndexIVFFlat + GPU index when corpus grows > 1M vectors
- Replace the entire class with a Pinecone / Weaviate / pgvector adapter
  that implements the same public interface (add_chunks, search, total_vectors)
"""

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import faiss
import numpy as np

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.document_processor import TextChunk

logger = get_logger(__name__)


@dataclass
class ChunkMetadata:
    """
    Metadata stored alongside each FAISS vector.

    Kept as a plain dataclass so it is trivially serialisable to/from JSON
    without an ORM.
    """

    document_id: str
    chunk_id: int
    source_filename: str
    text: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ChunkMetadata":
        return cls(**data)


class VectorStoreService:
    """
    Manages the FAISS index lifecycle: insert, search, persist, reload.

    Designed as an application-level singleton (see api/dependencies.py).
    Thread safety for writes is handled internally via a reentrant lock.
    """

    _INDEX_SUFFIX = ".index"

    def __init__(self, settings: Settings) -> None:
        self._index_path = Path(settings.FAISS_INDEX_PATH)
        self._metadata_path = Path(settings.METADATA_PATH)
        self._embedding_dim: Optional[int] = None
        self._index: Optional[faiss.IndexFlatIP] = None
        self._metadata: Dict[int, ChunkMetadata] = {}  # faiss_id → ChunkMetadata
        self._lock = threading.RLock()

        # Ensure parent directories exist before any I/O
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self._metadata_path.parent.mkdir(parents=True, exist_ok=True)

        self._load_from_disk()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_chunks(
        self,
        chunks: List[TextChunk],
        embeddings: List[List[float]],
    ) -> None:
        """
        Add text chunks and their embeddings to the index.

        Vectors are L2-normalised before insertion so that inner-product
        search is equivalent to cosine similarity.

        Args:
            chunks:     TextChunk objects carrying metadata.
            embeddings: One float vector per chunk (same order).

        Raises:
            ValueError: On dimension mismatch or count mismatch.
        """
        if not chunks:
            return
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"Chunk/embedding count mismatch: "
                f"{len(chunks)} chunks vs {len(embeddings)} embeddings."
            )

        vectors = np.array(embeddings, dtype="float32")
        faiss.normalize_L2(vectors)  # in-place L2 normalisation

        with self._lock:
            if self._index is None:
                self._initialise_index(dim=vectors.shape[1])

            if vectors.shape[1] != self._embedding_dim:
                raise ValueError(
                    f"Embedding dimension mismatch: "
                    f"index expects {self._embedding_dim}, got {vectors.shape[1]}."
                )

            base_id = self._index.ntotal
            self._index.add(vectors)

            for offset, chunk in enumerate(chunks):
                faiss_id = base_id + offset
                self._metadata[faiss_id] = ChunkMetadata(
                    document_id=chunk.document_id,
                    chunk_id=chunk.chunk_id,
                    source_filename=chunk.source_filename,
                    text=chunk.text,
                )

            logger.info(
                f"Added {len(chunks)} chunks. "
                f"Index total: {self._index.ntotal} vectors."
            )
            self._save_to_disk()

    def search(
        self,
        query_embedding: List[float],
        top_k: int,
    ) -> List[Tuple[ChunkMetadata, float]]:
        """
        Find the top_k most similar chunks to a query embedding.

        Args:
            query_embedding: Single query vector (must match index dimension).
            top_k:           Maximum number of results to return.

        Returns:
            List of (ChunkMetadata, cosine_score) tuples sorted by
            score descending. Returns [] when the index is empty.
        """
        with self._lock:
            if self._index is None or self._index.ntotal == 0:
                logger.warning("Vector store is empty — no results to return.")
                return []

            query = np.array([query_embedding], dtype="float32")
            faiss.normalize_L2(query)

            effective_k = min(top_k, self._index.ntotal)
            scores, indices = self._index.search(query, effective_k)

        results: List[Tuple[ChunkMetadata, float]] = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:  # FAISS uses -1 as a sentinel for empty slots
                continue
            metadata = self._metadata.get(int(idx))
            if metadata is not None:
                results.append((metadata, float(score)))

        logger.debug(
            f"FAISS search: {len(results)} candidates for top_k={top_k}"
        )
        return results

    @property
    def total_vectors(self) -> int:
        """Return the current number of vectors in the index."""
        with self._lock:
            return self._index.ntotal if self._index is not None else 0

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_to_disk(self) -> None:
        """
        Persist the FAISS index and metadata store.
        Must be called while holding self._lock.
        """
        if self._index is None:
            return

        index_file = str(self._index_path) + self._INDEX_SUFFIX
        faiss.write_index(self._index, index_file)

        serialised = {
            str(fid): meta.to_dict() for fid, meta in self._metadata.items()
        }
        with open(self._metadata_path, "w", encoding="utf-8") as fp:
            json.dump(serialised, fp, indent=2, ensure_ascii=False)

        logger.info(
            f"Persisted FAISS index ({self._index.ntotal} vectors) → {index_file}"
        )

    def _load_from_disk(self) -> None:
        """
        Reload a previously persisted index from disk on startup.
        Called once in __init__; safe to call again to force a reload.
        """
        index_file = Path(str(self._index_path) + self._INDEX_SUFFIX)

        if not index_file.exists() or not self._metadata_path.exists():
            logger.info(
                "No persisted vector store found — "
                "starting with an empty index."
            )
            return

        try:
            with self._lock:
                self._index = faiss.read_index(str(index_file))
                self._embedding_dim = self._index.d

                with open(self._metadata_path, "r", encoding="utf-8") as fp:
                    raw: Dict[str, dict] = json.load(fp)

                self._metadata = {
                    int(fid): ChunkMetadata.from_dict(meta)
                    for fid, meta in raw.items()
                }

            logger.info(
                f"Loaded FAISS index: {self._index.ntotal} vectors "
                f"(dim={self._embedding_dim}) from {index_file}"
            )
        except Exception as exc:
            logger.error(
                f"Failed to load persisted vector store: {exc}. "
                "Starting with an empty index."
            )
            self._index = None
            self._metadata = {}

    # ------------------------------------------------------------------
    # Index initialisation
    # ------------------------------------------------------------------

    def _initialise_index(self, dim: int) -> None:
        """Initialise a fresh IndexFlatIP. Must be called under self._lock."""
        self._embedding_dim = dim
        self._index = faiss.IndexFlatIP(dim)
        logger.info(f"Initialised new FAISS IndexFlatIP (dim={dim})")

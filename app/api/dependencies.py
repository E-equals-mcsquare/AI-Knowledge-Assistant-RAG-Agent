"""
app/api/dependencies.py

FastAPI dependency providers for all injectable services.

Pattern used:
  - Module-level lru_cache'd factory functions create application singletons
    (EmbeddingService, VectorStoreService / PineconeVectorStoreService, LLMService).
  - FastAPI Depends() wires these into route handlers without the route
    needing to know about construction details.
  - RetrievalService wraps the active vector store and is also cached.

Backend switching:
  Set VECTOR_STORE_BACKEND=faiss    → uses local FAISS (default, no extra config)
  Set VECTOR_STORE_BACKEND=pinecone → uses Pinecone (requires PINECONE_API_KEY)
  Set DOCUMENT_STORE_BACKEND=s3    → enables S3Service for presigned upload URLs
  No other file needs to change.

Testability:
  Call _get_vector_store.cache_clear() / get_settings.cache_clear() between
  test runs, or override via app.dependency_overrides in pytest fixtures.
"""

from functools import lru_cache
from typing import Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.embedding_service import EmbeddingService
from app.services.llm_service import LLMService
from app.services.retrieval_service import RetrievalService
from app.services.s3_service import S3Service
from app.services.vector_store import VectorStoreProtocol, VectorStoreService
from app.services.vector_store_pinecone import PineconeVectorStoreService

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_embedding_service() -> EmbeddingService:
    """Application-wide EmbeddingService singleton."""
    return EmbeddingService(settings=get_settings())


@lru_cache(maxsize=1)
def _get_vector_store() -> VectorStoreProtocol:
    """
    Return the configured vector store backend singleton.

    Reads VECTOR_STORE_BACKEND from settings:
      - "faiss"    → VectorStoreService     (local, no extra config)
      - "pinecone" → PineconeVectorStoreService (requires PINECONE_API_KEY)
    """
    settings = get_settings()
    backend = settings.VECTOR_STORE_BACKEND

    if backend == "pinecone":
        logger.info("Vector store backend: Pinecone")
        return PineconeVectorStoreService(settings=settings)

    logger.info("Vector store backend: FAISS (local)")
    return VectorStoreService(settings=settings)


@lru_cache(maxsize=1)
def _get_llm_service() -> LLMService:
    """Application-wide LLMService singleton."""
    return LLMService(settings=get_settings())


@lru_cache(maxsize=1)
def _get_retrieval_service() -> RetrievalService:
    """Application-wide RetrievalService singleton."""
    return RetrievalService(
        embedding_service=_get_embedding_service(),
        vector_store=_get_vector_store(),
        settings=get_settings(),
    )


@lru_cache(maxsize=1)
def _get_s3_service() -> Optional[S3Service]:
    """
    Return the S3Service singleton when DOCUMENT_STORE_BACKEND=s3, else None.

    S3Service is only instantiated when AWS_BUCKET_NAME is configured.
    Returns None in local mode so routes can remain unaware of the backend.
    """
    settings = get_settings()
    if settings.DOCUMENT_STORE_BACKEND == "s3":
        logger.info("Document store backend: S3 (presigned URL mode)")
        return S3Service(settings=settings)
    logger.info("Document store backend: local (synchronous in-process)")
    return None


# ---------------------------------------------------------------------------
# FastAPI dependency callables
# ---------------------------------------------------------------------------

def get_settings_dep() -> Settings:
    """Provide application settings."""
    return get_settings()


def get_embedding_service() -> EmbeddingService:
    """Provide the shared EmbeddingService."""
    return _get_embedding_service()


def get_vector_store() -> VectorStoreProtocol:
    """Provide the active vector store (FAISS or Pinecone)."""
    return _get_vector_store()


def get_llm_service() -> LLMService:
    """Provide the shared LLMService."""
    return _get_llm_service()


def get_retrieval_service() -> RetrievalService:
    """Provide the shared RetrievalService."""
    return _get_retrieval_service()


def get_s3_service() -> Optional[S3Service]:
    """Provide the S3Service when in s3 mode, or None in local mode."""
    return _get_s3_service()

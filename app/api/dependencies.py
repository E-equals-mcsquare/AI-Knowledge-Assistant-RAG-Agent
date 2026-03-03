"""
app/api/dependencies.py

FastAPI dependency providers for all injectable services.

Pattern used:
  - Module-level lru_cache'd factory functions create application singletons
    (EmbeddingService, VectorStoreService, LLMService) that are instantiated
    once and reused across all requests.
  - FastAPI Depends() wires these into route handlers without the route
    needing to know about construction details.
  - RetrievalService is the only "composite" dependency — it is also cached
    because it simply wraps already-cached sub-services.

Why lru_cache instead of FastAPI lifespan state?
  - Simpler: no need to store objects on app.state
  - Testable: call get_settings.cache_clear() / _get_vector_store.cache_clear()
    between test runs to get fresh instances.
  - Works correctly with FastAPI's dependency injection even outside a
    request context (e.g. CLI scripts, background tasks).

Future:
  - Swap VectorStoreService provider for a managed-DB adapter
    (Pinecone, pgvector) by changing only this file.
  - Add an async variant using AsyncOpenAI for streaming endpoints.
"""

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.services.embedding_service import EmbeddingService
from app.services.llm_service import LLMService
from app.services.retrieval_service import RetrievalService
from app.services.vector_store import VectorStoreService


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_embedding_service() -> EmbeddingService:
    """Application-wide EmbeddingService singleton."""
    return EmbeddingService(settings=get_settings())


@lru_cache(maxsize=1)
def _get_vector_store() -> VectorStoreService:
    """
    Application-wide VectorStoreService singleton.

    Loads the persisted FAISS index from disk on first call.
    """
    return VectorStoreService(settings=get_settings())


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


# ---------------------------------------------------------------------------
# FastAPI dependency callables
# These are the functions passed to Depends() in route handlers.
# Keeping them separate from the lru_cache'd factories makes type annotations
# cleaner and allows per-request override in tests via app.dependency_overrides.
# ---------------------------------------------------------------------------

def get_settings_dep() -> Settings:
    """Provide application settings."""
    return get_settings()


def get_embedding_service() -> EmbeddingService:
    """Provide the shared EmbeddingService."""
    return _get_embedding_service()


def get_vector_store() -> VectorStoreService:
    """Provide the shared VectorStoreService."""
    return _get_vector_store()


def get_llm_service() -> LLMService:
    """Provide the shared LLMService."""
    return _get_llm_service()


def get_retrieval_service() -> RetrievalService:
    """Provide the shared RetrievalService."""
    return _get_retrieval_service()

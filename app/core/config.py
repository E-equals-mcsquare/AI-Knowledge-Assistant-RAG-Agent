"""
app/core/config.py

Centralised application configuration loaded from environment variables.
All tuneable knobs live here — no hardcoded values anywhere else in the codebase.

Future: swap BaseSettings for AWS Secrets Manager / SSM Parameter Store
        by overriding the `settings_customise_sources` hook.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ------------------------------------------------------------------
    # OpenAI
    # ------------------------------------------------------------------
    OPENAI_API_KEY: str = Field(..., description="OpenAI API key")
    OPENAI_EMBEDDING_MODEL: str = Field(
        default="text-embedding-3-large",
        description="OpenAI model used for generating embeddings",
    )
    OPENAI_CHAT_MODEL: str = Field(
        default="gpt-4o",
        description="OpenAI model used for chat completions",
    )
    OPENAI_MAX_TOKENS: int = Field(
        default=1024,
        ge=64,
        le=8192,
        description="Maximum tokens for the LLM completion response",
    )
    OPENAI_TEMPERATURE: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
        description="Sampling temperature — lower = more deterministic",
    )

    # ------------------------------------------------------------------
    # Chunking
    # ------------------------------------------------------------------
    CHUNK_SIZE: int = Field(
        default=800,
        ge=100,
        le=4000,
        description="Approximate character count per chunk",
    )
    CHUNK_OVERLAP: int = Field(
        default=150,
        ge=0,
        le=800,
        description="Character overlap between consecutive chunks",
    )

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------
    TOP_K: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Default number of chunks to retrieve per query",
    )
    SIMILARITY_THRESHOLD: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum cosine similarity score required for a chunk to be "
            "considered relevant. Chunks below this threshold are discarded "
            "to prevent hallucination-prone retrievals."
        ),
    )

    # ------------------------------------------------------------------
    # Storage paths  (relative to project root)
    # ------------------------------------------------------------------
    FAISS_INDEX_PATH: str = Field(
        default="storage/faiss_index",
        description="File path prefix for the persisted FAISS index (no extension)",
    )
    METADATA_PATH: str = Field(
        default="storage/metadata.json",
        description="File path for the chunk metadata JSON store",
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    APP_ENV: str = Field(
        default="development",
        description="Runtime environment: development | staging | production",
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Python logging level: DEBUG | INFO | WARNING | ERROR",
    )
    MAX_UPLOAD_SIZE_MB: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum allowed upload file size in megabytes",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return a cached Settings instance.

    Using lru_cache ensures that .env is read only once at startup,
    and the same object is reused across the entire application lifetime.
    Call get_settings.cache_clear() in tests to reset between test runs.
    """
    return Settings()

"""
app/core/config.py

Centralised application configuration loaded from environment variables.
All tuneable knobs live here — no hardcoded values anywhere else in the codebase.
"""

import json
import logging
import os
from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_SECRETS_LOADED = False
_KEY_MAP = {
    "ai-knowledge-base-assistant-OPENAI_API_KEY": "OPENAI_API_KEY",
    "ai-knowledge-base-assistant-PINECONE_API_KEY": "PINECONE_API_KEY",
}


def _bootstrap_secrets() -> None:
    """Fetch secrets from AWS Secrets Manager and inject into os.environ.

    Only runs in production (APP_ENV=production). boto3 automatically uses
    the pod's IRSA token (AWS_WEB_IDENTITY_TOKEN_FILE + AWS_ROLE_ARN) injected
    by the EKS pod identity webhook — no extra configuration needed.
    """
    global _SECRETS_LOADED
    if _SECRETS_LOADED or os.environ.get("APP_ENV") != "production":
        return
    try:
        import boto3

        secret_name = "ai-knowledgebase-assistant-secrets"
        region = os.environ.get("AWS_REGION", "ap-south-1")
        client = boto3.session.Session().client("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=secret_name)
        secrets = json.loads(response["SecretString"])
        for secret_key, env_key in _KEY_MAP.items():
            if secret_key in secrets:
                os.environ[env_key] = secrets[secret_key]
        _SECRETS_LOADED = True
        logger.info("Secrets loaded from AWS Secrets Manager")
    except Exception as exc:
        logger.error("Failed to load secrets from Secrets Manager: %s", exc)
        raise


_bootstrap_secrets()


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
    # Vector store backend
    # ------------------------------------------------------------------
    VECTOR_STORE_BACKEND: Literal["faiss", "pinecone"] = Field(
        default="faiss",
        description="Vector store backend to use: 'faiss' (local) or 'pinecone' (managed)",
    )

    # ------------------------------------------------------------------
    # FAISS — used when VECTOR_STORE_BACKEND=faiss
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
    # Pinecone — used when VECTOR_STORE_BACKEND=pinecone
    # ------------------------------------------------------------------
    PINECONE_API_KEY: Optional[str] = Field(
        default=None,
        description="Pinecone API key (required when VECTOR_STORE_BACKEND=pinecone)",
    )
    PINECONE_INDEX_NAME: str = Field(
        default="knowledge-assistant",
        description="Name of the Pinecone index to use or create",
    )
    PINECONE_CLOUD: str = Field(
        default="aws",
        description="Cloud provider for Pinecone Serverless index (aws | gcp | azure)",
    )
    PINECONE_REGION: str = Field(
        default="us-east-1",
        description="Cloud region for the Pinecone Serverless index",
    )
    PINECONE_EMBEDDING_DIM: int = Field(
        default=3072,
        description=(
            "Embedding dimension used when creating the Pinecone index. "
            "Must match the embedding model: "
            "text-embedding-3-large=3072, text-embedding-3-small=1536"
        ),
    )

    # ------------------------------------------------------------------
    # AWS — used when DOCUMENT_STORE_BACKEND=s3
    # ------------------------------------------------------------------
    DOCUMENT_STORE_BACKEND: Literal["local", "s3"] = Field(
        default="local",
        description="Where uploaded files are stored: 'local' (in-process) or 's3' (async via Lambda)",
    )
    AWS_REGION: str = Field(
        default="us-east-1",
        description="AWS region for S3 and Lambda",
    )
    AWS_BUCKET_NAME: Optional[str] = Field(
        default=None,
        description="S3 bucket name for document storage (required when DOCUMENT_STORE_BACKEND=s3)",
    )
    AWS_S3_PREFIX: str = Field(
        default="documents/",
        description="S3 key prefix (folder) for uploaded documents",
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
    return Settings()  # type: ignore[call-arg]  # pydantic-settings populates fields from env vars at runtime

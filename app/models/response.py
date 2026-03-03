"""
app/models/response.py

Pydantic response schemas for all API endpoints.

All fields are typed and described so that the auto-generated OpenAPI spec
(/docs) is self-documenting for API consumers.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class ChunkSource(BaseModel):
    """
    Identifies a single document chunk that contributed to an answer.
    Included in every ChatResponse so callers can trace answers back to source.
    """

    document: str = Field(
        ...,
        description="Original filename of the source document.",
    )
    chunk_id: int = Field(
        ...,
        description="Zero-based index of the chunk within its source document.",
    )
    score: Optional[float] = Field(
        default=None,
        description=(
            "Cosine similarity score between the query and this chunk "
            "(range 0–1). Higher = more relevant."
        ),
    )


class ChatResponse(BaseModel):
    """
    Response returned by POST /chat.

    `sources` is empty when no relevant chunks were found, signalling that
    the answer is the fallback 'not found' message rather than LLM output.
    """

    answer: str = Field(
        ...,
        description="LLM-generated answer grounded strictly in retrieved context.",
    )
    sources: List[ChunkSource] = Field(
        default_factory=list,
        description="Ordered list of document chunks used to generate the answer.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "answer": (
                        "According to the runbook, database failover is handled "
                        "automatically by RDS Multi-AZ. The standby replica is "
                        "promoted within 60–120 seconds of a primary failure."
                    ),
                    "sources": [
                        {"document": "db-runbook.pdf", "chunk_id": 4, "score": 0.8712},
                        {"document": "infra-overview.txt", "chunk_id": 11, "score": 0.7934},
                    ],
                }
            ]
        }
    }


class UploadResponse(BaseModel):
    """
    Response returned by POST /upload on successful document ingestion.
    """

    document_id: str = Field(
        ...,
        description="UUID assigned to the ingested document.",
    )
    filename: str = Field(
        ...,
        description="Original filename of the uploaded document.",
    )
    chunks_indexed: int = Field(
        ...,
        description="Number of text chunks embedded and stored in the vector index.",
    )
    message: str = Field(
        ...,
        description="Human-readable status message.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "document_id": "3f7e1b2a-84c0-4d9e-b5a1-0f2c8e6d4321",
                    "filename": "architecture-guide.pdf",
                    "chunks_indexed": 42,
                    "message": "Document 'architecture-guide.pdf' successfully ingested with 42 chunks.",
                }
            ]
        }
    }


class HealthResponse(BaseModel):
    """Response returned by GET /health."""

    status: str
    service: str
    version: str
    vector_store_size: int = Field(
        ...,
        description="Number of vectors currently stored in the FAISS index.",
    )

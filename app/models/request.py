"""
app/models/request.py

Pydantic request schemas for all API endpoints.

Keeping request and response models in a dedicated layer makes it trivial
to version the API (v1/v2) and decouple wire format from internal logic.
"""

from typing import Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """
    Payload accepted by POST /chat.

    The optional `top_k` field allows per-request overrides of the global
    TOP_K configuration — useful for callers that want broader or narrower
    retrieval without redeploying the service.
    """

    question: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Natural language question to answer from the knowledge base.",
    )
    top_k: Optional[int] = Field(
        default=None,
        ge=1,
        le=20,
        description=(
            "Number of document chunks to retrieve. "
            "Overrides the TOP_K environment variable when provided."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "question": "How do we handle database failover?",
                },
                {
                    "question": "What is the on-call escalation process?",
                    "top_k": 8,
                },
            ]
        }
    }

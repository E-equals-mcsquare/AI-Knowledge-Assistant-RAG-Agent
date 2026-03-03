"""
app/api/routes/chat.py

POST /chat  —  Ask a question against the knowledge base.

Full RAG pipeline per request:
  1. Retrieve top-k relevant chunks (embedding + FAISS search + threshold filter)
  2. If no chunks pass the threshold → return "not found" response (no LLM call)
  3. Build a grounded prompt from the retrieved context
  4. Call the LLM and return a structured answer with source citations

Hallucination prevention:
  - The similarity threshold filter (SIMILARITY_THRESHOLD env var) discards
    low-relevance chunks before they reach the prompt.
  - The system prompt explicitly forbids the LLM from using parametric knowledge.
  - When the vector store is empty or no chunks pass the threshold, a hard-coded
    "not found" message is returned — no LLM call is made.

Error handling:
  - 422  Validation Error  — invalid request body (handled by FastAPI/Pydantic)
  - 500  Internal Error    — retrieval or LLM API failure
"""

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_llm_service, get_retrieval_service
from app.core.logging import get_logger
from app.models.request import ChatRequest
from app.models.response import ChatResponse, ChunkSource
from app.services.llm_service import LLMService
from app.services.prompt_builder import build_messages
from app.services.retrieval_service import RetrievalService

router = APIRouter()
logger = get_logger(__name__)

# Hard-coded fallback: returned without calling the LLM when no relevant
# context is found.  Keep this consistent with the system prompt wording.
_NOT_FOUND_ANSWER = "Information not found in knowledge base."


@router.post(
    "/chat",
    response_model=ChatResponse,
    status_code=200,
    summary="Ask a question against the knowledge base",
    description=(
        "Embeds the question, retrieves relevant document chunks, and "
        "generates a grounded answer using the LLM. "
        "Returns a fallback message if no relevant context is found."
    ),
    responses={
        200: {"description": "Answer generated (or 'not found' fallback)"},
        500: {"description": "Retrieval or LLM generation failure"},
    },
)
async def chat(
    request: ChatRequest,
    retrieval_service: RetrievalService = Depends(get_retrieval_service),
    llm_service: LLMService = Depends(get_llm_service),
) -> ChatResponse:
    """
    RAG query pipeline.

    Steps:
    1. Retrieve relevant chunks for the question
    2. Return fallback if no chunks pass the similarity threshold
    3. Build grounded prompt from chunks
    4. Generate LLM answer
    5. Return structured response with source citations
    """
    logger.info(
        f"Chat request | question='{request.question[:100]}"
        f"{'...' if len(request.question) > 100 else ''}' | "
        f"top_k_override={request.top_k}"
    )

    # --- Step 1: Retrieval ---
    try:
        retrieved_chunks = retrieval_service.retrieve(
            query=request.question,
            top_k=request.top_k,
        )
    except Exception as exc:
        logger.error(f"Retrieval failed: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"Retrieval error: {str(exc)}",
        )

    # --- Step 2: Hallucination gate ---
    if not retrieved_chunks:
        logger.info(
            "No chunks passed similarity threshold — returning fallback response."
        )
        return ChatResponse(answer=_NOT_FOUND_ANSWER, sources=[])

    # --- Step 3 & 4: Prompt + generation ---
    try:
        messages = build_messages(
            question=request.question,
            chunks=retrieved_chunks,
        )
        answer = llm_service.generate(messages=messages)
    except Exception as exc:
        logger.error(f"LLM generation failed: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"Answer generation error: {str(exc)}",
        )

    # --- Step 5: Build structured response ---
    sources = [
        ChunkSource(
            document=chunk.metadata.source_filename,
            chunk_id=chunk.metadata.chunk_id,
            score=round(chunk.score, 4),
        )
        for chunk in retrieved_chunks
    ]

    logger.info(
        f"Chat response generated | sources={len(sources)} | "
        f"answer_length={len(answer)} chars"
    )

    return ChatResponse(answer=answer, sources=sources)

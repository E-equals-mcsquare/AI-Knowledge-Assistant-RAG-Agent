"""
app/services/prompt_builder.py

Constructs the LLM prompt from retrieved chunks and the user's question.

Prompting strategy:
- System prompt anchors the model strictly to provided context and explicitly
  forbids speculation or use of parametric knowledge.
- Each retrieved chunk is labelled with its source and chunk ID so the model
  can reference them naturally in its answer.
- Context chunks are separated by a visible delimiter (---) to help the model
  distinguish between sources.
- The "Answer:" suffix nudges the model to produce a direct, grounded reply.

This module is pure functions — no state, no I/O — making it trivially testable.

Future:
- Add token counting (tiktoken) to enforce a hard context-window budget and
  truncate the lowest-scoring chunks when the prompt is too long.
- Support multi-turn conversation by accepting a history parameter and
  injecting prior turns as assistant messages.
"""

from typing import List

from app.core.logging import get_logger
from app.services.retrieval_service import RetrievedChunk

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# System prompt — this is the single source of grounding truth for the LLM.
# Edit carefully: even small wording changes can shift model behaviour.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are an AI Knowledge Assistant for an engineering team.

Your ONLY job is to answer questions using the context passages provided \
below. Follow these rules without exception:

1. Base every answer EXCLUSIVELY on the provided context.
2. Do NOT use your general training knowledge, prior world knowledge, or \
any information not present in the context.
3. If the answer cannot be found in the context, respond with EXACTLY:
   "I'm sorry, but I couldn't find relevant information in the knowledge \
base to answer your question."
4. Do not speculate, infer beyond what is explicitly stated, or make \
assumptions to fill gaps.
5. Be precise, technical, and concise. Prefer bullet points for \
multi-step answers.
6. When referencing information you may say "According to the documentation…" \
but do not fabricate source names.
"""

_CONTEXT_SEPARATOR = "\n\n---\n\n"


def build_context_block(chunks: List[RetrievedChunk]) -> str:
    """
    Format retrieved chunks into a numbered context block.

    Each chunk is prefixed with its source filename and chunk ID so the
    model can reference them and so log analysis can trace back to raw data.

    Args:
        chunks: Ordered list of retrieved chunks (highest score first).

    Returns:
        Formatted multi-chunk context string, or a fallback message if empty.
    """
    if not chunks:
        return "No relevant context available."

    parts: List[str] = []
    for i, chunk in enumerate(chunks, start=1):
        header = (
            f"[Context {i}] "
            f"Source: {chunk.metadata.source_filename} | "
            f"Chunk ID: {chunk.metadata.chunk_id}"
        )
        parts.append(f"{header}\n{chunk.metadata.text.strip()}")

    return _CONTEXT_SEPARATOR.join(parts)


def build_messages(question: str, chunks: List[RetrievedChunk]) -> List[dict]:
    """
    Assemble the full messages array for the OpenAI Chat Completions API.

    Structure:
        [
          {"role": "system", "content": <SYSTEM_PROMPT>},
          {"role": "user",   "content": <context + question>},
        ]

    Args:
        question: The user's natural language question.
        chunks:   Retrieved chunks to include as context.

    Returns:
        Messages list ready to pass to LLMService.generate().
    """
    context_block = build_context_block(chunks)

    user_content = (
        f"Context:\n{context_block}\n\n"
        f"Question: {question}\n\n"
        f"Answer:"
    )

    logger.debug(
        f"Prompt built | chunks={len(chunks)} | "
        f"user_content_length={len(user_content)} chars"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

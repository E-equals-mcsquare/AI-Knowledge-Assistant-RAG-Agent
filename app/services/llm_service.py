"""
app/services/llm_service.py

Wraps the OpenAI Chat Completions API for generating grounded answers.

Design notes:
- Stateless beyond the shared OpenAI client — safe as an application singleton.
- All prompt construction is delegated to prompt_builder.py; this service
  only handles the API interaction.
- OpenAIError is re-raised to let the route layer convert it to an HTTP 500.

Future:
- Add async generate_async() using the AsyncOpenAI client for streaming support
- Add retry logic with exponential backoff (tenacity) for transient 429/5xx errors
- Support function-calling / tool-use for agentic RAG workflows
"""

from typing import List

from openai import OpenAI, OpenAIError

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class LLMService:
    """
    Generates answers via the OpenAI Chat Completions API.

    Instantiated once and reused for the application lifetime.
    """

    def __init__(self, settings: Settings) -> None:
        self._client = OpenAI(api_key=settings.OPENAI_API_KEY)
        self._model = settings.OPENAI_CHAT_MODEL
        self._max_tokens = settings.OPENAI_MAX_TOKENS
        self._temperature = settings.OPENAI_TEMPERATURE

    def generate(self, messages: List[dict]) -> str:
        """
        Call the Chat Completions endpoint and return the assistant's reply.

        Args:
            messages: OpenAI messages array, e.g.
                      [{"role": "system", "content": "..."},
                       {"role": "user",   "content": "..."}]

        Returns:
            The assistant's response text (stripped).

        Raises:
            OpenAIError: Propagated as-is so the caller can decide how to handle.
        """
        try:
            logger.debug(
                f"Calling {self._model} | "
                f"messages={len(messages)} | "
                f"max_tokens={self._max_tokens} | "
                f"temperature={self._temperature}"
            )

            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )

            answer = response.choices[0].message.content.strip()
            usage = response.usage

            logger.info(
                f"LLM response | model={self._model} | "
                f"prompt_tokens={usage.prompt_tokens} | "
                f"completion_tokens={usage.completion_tokens} | "
                f"total_tokens={usage.total_tokens}"
            )
            return answer

        except OpenAIError as exc:
            logger.error(f"OpenAI chat completion error: {exc}")
            raise

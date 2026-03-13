"""
app/core/metrics.py

Prometheus metrics for the AI Knowledge Assistant RAG pipeline.

Metrics exposed:
  rag_requests_total                  — Request counter by status (success/error)
  rag_request_latency_seconds         — End-to-end request latency
  vector_search_latency_seconds       — Pinecone retrieval latency
  llm_generation_latency_seconds      — OpenAI generation latency
  llm_tokens_total                    — Token usage by type (prompt/completion)

All metrics are registered in the default prometheus_client registry and
exposed at /metrics by prometheus_fastapi_instrumentator in main.py.
"""

from prometheus_client import Counter, Histogram

rag_requests_total = Counter(
    "rag_requests_total",
    "Total number of RAG /chat requests",
    ["status"],   # "success" | "error"
)

rag_request_latency_seconds = Histogram(
    "rag_request_latency_seconds",
    "End-to-end /chat request latency in seconds",
    buckets=[0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

vector_search_latency_seconds = Histogram(
    "vector_search_latency_seconds",
    "Vector store retrieval latency in seconds",
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)

llm_generation_latency_seconds = Histogram(
    "llm_generation_latency_seconds",
    "LLM answer generation latency in seconds",
    buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0],
)

llm_tokens_total = Counter(
    "llm_tokens_total",
    "Total LLM tokens consumed",
    ["type"],     # "prompt" | "completion"
)

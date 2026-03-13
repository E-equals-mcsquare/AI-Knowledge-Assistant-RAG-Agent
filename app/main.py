"""
app/main.py

FastAPI application entrypoint.

Responsibilities:
  - Create and configure the FastAPI app instance
  - Register CORS middleware
  - Mount all routers
  - Define the lifespan context (startup / shutdown hooks)
  - Expose /health and /docs endpoints

CORS:
  allow_origins=["*"] is intentional for development. In production,
  replace "*" with the specific frontend origins or proxy domain.

Lifespan:
  Pre-warms all singleton services on startup so the first real request
  doesn't pay a cold-start penalty (especially FAISS index loading).

Future:
  - Add Prometheus metrics middleware (prometheus-fastapi-instrumentator)
  - Add request-ID middleware for distributed tracing correlation
  - Add rate-limiting middleware (slowapi) for the /upload endpoint
  - Replace allow_origins=["*"] with an env-var-driven allowlist
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.dependencies import (
    _get_embedding_service,
    _get_llm_service,
    _get_retrieval_service,
    _get_vector_store,
)
from app.api.routes import chat, upload
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

# Configure logging before anything else so startup messages are captured.
configure_logging()
logger = get_logger(__name__)
settings = get_settings()


# ---------------------------------------------------------------------------
# Lifespan: startup + shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Application lifecycle manager.

    Startup:
      - Logs configuration summary
      - Pre-warms all singleton services (FAISS load, client initialisation)

    Shutdown:
      - Logs graceful shutdown message
      - (Future) flush metrics, close DB connections, etc.
    """
    # --- Startup ---
    logger.info("=" * 60)
    logger.info("Starting AI Knowledge Assistant")
    logger.info(f"  Environment  : {settings.APP_ENV}")
    logger.info(f"  Embedding    : {settings.OPENAI_EMBEDDING_MODEL}")
    logger.info(f"  Chat model   : {settings.OPENAI_CHAT_MODEL}")
    logger.info(f"  Chunk size   : {settings.CHUNK_SIZE} chars  |  overlap: {settings.CHUNK_OVERLAP}")
    logger.info(f"  Top-K        : {settings.TOP_K}  |  threshold: {settings.SIMILARITY_THRESHOLD}")
    logger.info(f"  FAISS index  : {settings.FAISS_INDEX_PATH}.index")
    logger.info("=" * 60)

    # Pre-warm singletons to surface config errors before the first request
    try:
        vector_store = _get_vector_store()
        _get_embedding_service()
        _get_llm_service()
        _get_retrieval_service()
        logger.info(
            f"Services ready | vector_store vectors={vector_store.total_vectors}"
        )
    except Exception as exc:
        logger.error(f"Service initialisation failed: {exc}")
        raise

    yield

    # --- Shutdown ---
    logger.info("Shutting down AI Knowledge Assistant — goodbye.")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AI Knowledge Assistant",
    description=(
        "Internal RAG-powered knowledge assistant for engineering teams.\n\n"
        "**Endpoints**\n"
        "- `POST /upload` — Ingest a PDF or TXT document\n"
        "- `POST /chat`   — Ask a question grounded in ingested documents\n"
        "- `GET  /health` — Health check\n"
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # TODO: restrict to known origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Global exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Catch-all for any unhandled exceptions not already converted to HTTPException.
    Returns a sanitised 500 response to avoid leaking internal stack traces.
    """
    logger.error(
        f"Unhandled exception on {request.method} {request.url.path}: "
        f"{type(exc).__name__}: {exc}",
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected internal error occurred. Please try again later."},
    )

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(upload.router, tags=["Ingestion"])
app.include_router(chat.router, tags=["Retrieval & Generation"])

# ---------------------------------------------------------------------------
# Prometheus metrics — exposes /metrics for Prometheus scraping
# ---------------------------------------------------------------------------

Instrumentator().instrument(app).expose(app)

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    tags=["Health"],
    summary="Service health check",
    description=(
        "Returns 200 when the service is running. "
        "Also reports the current vector store size for operational visibility."
    ),
)
async def health_check() -> dict:
    """
    Lightweight health probe for load balancers and container orchestration
    (ECS, EKS, Lambda function URLs).
    """
    vector_store = _get_vector_store()
    return {
        "status": "healthy",
        "service": "ai-knowledge-assistant",
        "version": "1.0.0",
        "environment": settings.APP_ENV,
        "vector_store_size": vector_store.total_vectors,
    }

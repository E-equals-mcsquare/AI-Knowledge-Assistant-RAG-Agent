# ============================================================
# AI Knowledge Assistant — Dockerfile
# ============================================================
# Multi-stage build:
#   builder  — installs Python dependencies into a clean prefix
#   runtime  — minimal image with only the installed packages + app code
#
# Build:
#   docker build -t ai-knowledge-assistant .
#
# Run (local mode, FAISS):
#   docker run --rm -p 8000:8000 \
#     --env-file .env \
#     -v $(pwd)/storage:/app/storage \
#     ai-knowledge-assistant
#
# Run (s3 + Pinecone, credentials from host AWS CLI):
#   docker run --rm -p 8000:8000 \
#     --env-file .env \
#     -v ~/.aws:/home/appuser/.aws:ro \
#     ai-knowledge-assistant
# ============================================================


# ------------------------------------------------------------
# Stage 1: builder
# ------------------------------------------------------------
FROM python:3.13-slim AS builder

# Keep pip quiet and avoid writing .pyc files during install
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY requirements.txt .

# Install all dependencies into /install so the runtime stage
# can copy only that directory (no build tooling needed at runtime)
RUN pip install --prefix=/install -r requirements.txt


# ------------------------------------------------------------
# Stage 2: runtime
# ------------------------------------------------------------
FROM python:3.13-slim AS runtime

# --- Security: run as a non-root user ---
RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --no-create-home appuser

# Pull in the installed packages from the builder stage
COPY --from=builder /install /usr/local

# --- App environment ---
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    # pydantic-settings: skip .env in container (vars injected by orchestrator)
    # Set to "1" only if you want .env inside the image (not recommended)
    DOTENV_IGNORE=0

WORKDIR /app

# Copy application source
COPY app/ ./app/

# Create the storage directory for FAISS index + metadata (local mode).
# In production (s3 mode), this directory stays empty but must exist so
# the process doesn't fail on startup path resolution.
RUN mkdir -p storage && chown -R appuser:appgroup /app

USER appuser

EXPOSE 8000

# Uvicorn:
#   --host 0.0.0.0    — bind to all interfaces inside the container
#   --workers 1       — single worker; FAISS index is not fork-safe with >1 worker
#   --proxy-headers   — trust X-Forwarded-* from ALB / CloudFront
#   --forwarded-allow-ips='*' — required when running behind a proxy
ENTRYPOINT ["python", "-m", "uvicorn", "app.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips=*"]

"""
lambda/document_processor.py

AWS Lambda handler — triggered by S3 PutObject events.

Flow:
  Cold start: Secrets Manager → inject ai-knowledge-base-assistant-OPENAI_API_KEY + ai-knowledge-base-assistant-PINECONE_API_KEY into os.environ
  Per invocation: S3 event → parse key → download → extract → chunk → embed → Pinecone upsert

Secret management:
  ai-knowledge-base-assistant-OPENAI_API_KEY and ai-knowledge-base-assistant-PINECONE_API_KEY are fetched from AWS Secrets Manager at cold
  start and injected into os.environ so that pydantic-settings (Settings) picks them
  up transparently. The Secrets Manager call is made exactly once per Lambda container
  lifetime — subsequent warm invocations skip it entirely.

  Expected secret format (JSON string stored in Secrets Manager):
    {
      "ai-knowledge-base-assistant-OPENAI_API_KEY":  "sk-...",
      "ai-knowledge-base-assistant-PINECONE_API_KEY": "pcsk_..."
    }

Lambda environment variables (set in the Lambda configuration console):
  SECRETS_MANAGER_SECRET_NAME  — name/ARN of the secret (default: ai-knowledgebase-assistant-secrets)
  AWS_REGION                   — region where the secret lives (default: ap-south-1)
  OPENAI_EMBEDDING_MODEL       — e.g. text-embedding-3-large
  PINECONE_INDEX_NAME          — target Pinecone index
  PINECONE_CLOUD               — aws | gcp | azure
  PINECONE_REGION              — e.g. us-east-1
  PINECONE_EMBEDDING_DIM       — must match the embedding model (3072 for 3-large)
  AWS_BUCKET_NAME              — S3 bucket that triggers this Lambda
  AWS_S3_PREFIX                — key prefix used to parse document_id from the S3 key
  CHUNK_SIZE                   — (optional) default 800
  CHUNK_OVERLAP                — (optional) default 150

IAM permissions required on the Lambda execution role:
  - secretsmanager:GetSecretValue  on the secret ARN
  - s3:GetObject                   on the source bucket
  - Internet access (or VPC endpoints) for OpenAI + Pinecone APIs

S3 key contract (must match S3Service._build_key):
  {AWS_S3_PREFIX}{document_id}/{original_filename}
  e.g. documents/3f7e1b2a-84c0-4d9e-b5a1-0f2c8e6d4321/architecture-guide.pdf
"""

import json
import os
import urllib.parse
from typing import Any, Dict

from boto3.session import Session
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Cold-start secret injection
# Must run before any app.* imports so that get_settings() sees the injected values.
# ---------------------------------------------------------------------------

_SECRETS_LOADED = False  # module-level flag — True after first successful load


def _load_secrets_to_env() -> None:
    """
    Fetch ai-knowledge-base-assistant-OPENAI_API_KEY and ai-knowledge-base-assistant-PINECONE_API_KEY from AWS Secrets Manager and
    inject them into os.environ.

    Called once at module import time (cold start). Subsequent warm invocations
    skip this function entirely via the _SECRETS_LOADED guard.

    The secret must be stored as a JSON string:
        {"ai-knowledge-base-assistant-OPENAI_API_KEY": "sk-...", "ai-knowledge-base-assistant-PINECONE_API_KEY": "pcsk_..."}

    Raises:
        RuntimeError: If the secret cannot be fetched or parsed, so that
                      Lambda fails fast rather than running with missing credentials.
    """
    global _SECRETS_LOADED
    if _SECRETS_LOADED:
        return

    secret_name = os.environ.get(
        "SECRETS_MANAGER_SECRET_NAME",
        "ai-knowledgebase-assistant-secrets",
    )
    region = os.environ.get("AWS_REGION", "ap-south-1")

    print(f"[secrets] Fetching secret '{secret_name}' from region '{region}'")

    session = Session()
    client = session.client(service_name="secretsmanager", region_name=region)

    try:
        response = client.get_secret_value(SecretId=secret_name)
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        raise RuntimeError(
            f"Secrets Manager error ({error_code}) fetching '{secret_name}': {exc}"
        ) from exc

    raw = response.get("SecretString")
    if not raw:
        raise RuntimeError(
            f"Secret '{secret_name}' has no SecretString. "
            "Store the secret as a plaintext JSON string."
        )

    try:
        secrets: Dict[str, str] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Secret '{secret_name}' is not valid JSON: {exc}"
        ) from exc

    # Map Secrets Manager key names → the env var names pydantic-settings expects.
    # The secret stores keys with the app prefix; Settings reads the standard names.
    _KEY_MAP = {
        "ai-knowledge-base-assistant-OPENAI_API_KEY": "OPENAI_API_KEY",
        "ai-knowledge-base-assistant-PINECONE_API_KEY": "PINECONE_API_KEY",
    }

    missing = [k for k in _KEY_MAP if k not in secrets]
    if missing:
        raise RuntimeError(
            f"Secret '{secret_name}' is missing required keys: {missing}. "
            f"Found: {list(secrets.keys())}"
        )

    for secret_key, env_key in _KEY_MAP.items():
        os.environ[env_key] = secrets[secret_key]

    print(f"[secrets] Injected: {list(_KEY_MAP.values())}")
    _SECRETS_LOADED = True


# Run at import time (cold start). Warm invocations skip via _SECRETS_LOADED guard.
_load_secrets_to_env()


# ---------------------------------------------------------------------------
# App imports — must come AFTER secret injection so Settings sees the env vars
# ---------------------------------------------------------------------------

from app.core.config import get_settings  # noqa: E402
from app.core.logging import get_logger  # noqa: E402
from app.services.document_processor import DocumentProcessor  # noqa: E402
from app.services.embedding_service import EmbeddingService  # noqa: E402
from app.services.s3_service import S3Service  # noqa: E402
from app.services.vector_store_pinecone import PineconeVectorStoreService  # noqa: E402

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Service singletons — initialised once per container (warm reuse)
# ---------------------------------------------------------------------------

_settings = get_settings()
_s3_service = S3Service(settings=_settings)
_embedding_service = EmbeddingService(settings=_settings)
_vector_store = PineconeVectorStoreService(settings=_settings)
_processor = DocumentProcessor(settings=_settings)


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda entry point.

    Receives an S3 event notification and processes the uploaded document
    through the full ingestion pipeline: download → extract → chunk → embed → upsert.

    Args:
        event:   AWS S3 event payload (see S3 Event Notifications docs).
        context: Lambda context object (used for request ID in logs).

    Returns:
        A dict with statusCode 200 on success, or raises on hard failure
        (Lambda will retry based on the event source mapping configuration).
    """
    request_id = getattr(context, "aws_request_id", "local")
    logger.info(f"Lambda invoked | request_id={request_id}")

    records = event.get("Records", [])
    if not records:
        logger.warning("Event contained no Records — nothing to process.")
        return {"statusCode": 200, "body": "no records"}

    processed = 0
    errors = []

    for record in records:
        s3_key = _extract_s3_key(record)
        if not s3_key:
            logger.warning(f"Could not extract S3 key from record: {record}")
            continue

        try:
            _process_record(s3_key)
            processed += 1
        except Exception as exc:
            logger.error(f"Failed to process '{s3_key}': {exc}", exc_info=True)
            errors.append({"key": s3_key, "error": str(exc)})

    if errors:
        # Surface failures so Lambda's retry / DLQ mechanism can act on them.
        raise RuntimeError(
            f"Processed {processed} records; {len(errors)} failed: "
            + json.dumps(errors)
        )

    logger.info(f"Lambda complete | processed={processed} records")
    return {"statusCode": 200, "body": f"processed {processed} document(s)"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_s3_key(record: Dict[str, Any]) -> str:
    """
    Pull the S3 object key from a single S3 event record.

    S3 URL-encodes the key (spaces → '+', special chars → '%XX').
    urllib.parse.unquote_plus reverses this so the key matches what we stored.
    """
    try:
        raw_key = record["s3"]["object"]["key"]
        return urllib.parse.unquote_plus(raw_key)
    except (KeyError, TypeError):
        return ""


def _process_record(s3_key: str) -> None:
    """
    Full ingestion pipeline for a single S3 object.

    Steps:
      1. Parse document_id + filename from the S3 key.
      2. Download raw bytes from S3.
      3. Extract text (PDF or TXT).
      4. Chunk the text with sliding-window overlap.
      5. Generate embeddings (batched OpenAI calls).
      6. Upsert vectors + metadata to Pinecone.

    Raises:
        RuntimeError: On S3 download failure.
        ValueError:   On unsupported file type or empty document.
    """
    # --- 1. Parse key ---
    key_components = _s3_service.parse_key(s3_key)
    document_id = key_components.document_id
    filename = key_components.filename

    logger.info(
        f"Processing | doc_id={document_id} | file='{filename}' | key={s3_key}"
    )

    # --- 2. Download ---
    file_bytes = _s3_service.download_document(s3_key)
    logger.info(f"Downloaded {len(file_bytes) / 1024:.1f} KB from S3")

    # --- 3. Extract text ---
    try:
        raw_text = _processor.extract_text(file_bytes, filename)
    except ValueError as exc:
        raise ValueError(f"Text extraction failed for '{filename}': {exc}") from exc

    if not raw_text.strip():
        raise ValueError(
            f"No extractable text in '{filename}'. "
            "The file may be empty, scanned (image-only PDF), or corrupted."
        )

    # --- 4. Chunk ---
    chunks = _processor.chunk_text(
        text=raw_text,
        document_id=document_id,
        source_filename=filename,
    )
    if not chunks:
        raise ValueError(f"Could not produce any chunks from '{filename}'.")

    logger.info(f"Chunked into {len(chunks)} segments")

    # --- 5. Embed ---
    embeddings = _embedding_service.embed_texts([c.text for c in chunks])

    # --- 6. Upsert ---
    _vector_store.add_chunks(chunks, embeddings)

    logger.info(
        f"Ingestion complete | doc_id={document_id} | "
        f"file='{filename}' | chunks={len(chunks)}"
    )

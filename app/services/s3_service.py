"""
app/services/s3_service.py

Amazon S3 service — generates presigned upload URLs and downloads documents.

Architecture:
  The API server never touches the file bytes for uploads.
  Instead it generates a presigned S3 PUT URL that the client (React / curl)
  uses to upload directly to S3, bypassing API Gateway size limits entirely.

  POST /upload  →  presigned PUT URL  →  client uploads to S3  →  S3 event  →  Lambda

Credentials:
  boto3 resolves credentials via its standard chain — NO keys in .env needed:
    1. IAM role attached to the ECS task / Lambda  (production)
    2. ~/.aws/credentials or `aws configure`        (local dev / AWS CLI)
    3. AWS_PROFILE env var                          (CI / named profiles)
  For local dev just run:  aws configure  (one-time setup)

S3 key format:
    {AWS_S3_PREFIX}{document_id}/{original_filename}
    e.g. documents/3f7e1b2a-84c0/architecture-guide.pdf

  The Lambda handler reconstructs document_id and filename from the key alone.

Future:
  - Add presigned GET URLs for secure document retrieval
  - Add SSE-KMS encryption config to put_object params
  - Add Content-MD5 / checksum enforcement on the presigned URL
"""

from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_CONTENT_TYPE_MAP = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
}

_DEFAULT_EXPIRY_SECONDS = 900  # 15 minutes — enough for large files on slow connections


@dataclass
class PresignedUploadURL:
    """Everything the client needs to upload a file directly to S3."""

    presigned_url: str        # HTTP PUT target — contains signed credentials in query string
    s3_key: str               # Full S3 object key (sent back so client can reference it)
    document_id: str          # UUID assigned before upload
    filename: str             # Original filename
    expires_in: int           # URL validity in seconds
    http_method: str = "PUT"  # Client must use PUT, not POST


@dataclass
class S3KeyComponents:
    """Parts decoded from a structured S3 key."""

    document_id: str
    filename: str


class S3Service:
    """
    Manages presigned S3 upload URLs and document downloads.

    Application-level singleton — holds only a stateless boto3 client.
    Credentials come exclusively from the IAM role / AWS CLI — never from .env.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.AWS_BUCKET_NAME:
            raise ValueError(
                "AWS_BUCKET_NAME is required when DOCUMENT_STORE_BACKEND=s3. "
                "Set it in your .env file."
            )

        self._bucket = settings.AWS_BUCKET_NAME
        self._prefix = settings.AWS_S3_PREFIX.rstrip("/") + "/"
        self._region = settings.AWS_REGION
        # boto3 picks up credentials from IAM role / AWS CLI — no keys stored here
        self._client = boto3.client("s3", region_name=self._region)

        logger.info(
            f"S3Service ready | bucket={self._bucket} | "
            f"prefix={self._prefix} | region={self._region}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_upload_url(
        self,
        filename: str,
        document_id: str,
        expires_in: int = _DEFAULT_EXPIRY_SECONDS,
    ) -> PresignedUploadURL:
        """
        Generate a presigned S3 PUT URL for a document.

        The client uses this URL to upload the file directly to S3 — the API
        server is not involved in the actual file transfer, keeping it stateless
        and removing API Gateway payload limits.

        Args:
            filename:    Original filename (preserved in the S3 key).
            document_id: UUID assigned to this document (generated before calling).
            expires_in:  URL validity window in seconds (default: 15 min).

        Returns:
            PresignedUploadURL containing the URL and metadata.

        Raises:
            RuntimeError: If S3 fails to generate the presigned URL.
        """
        s3_key = self._build_key(document_id, filename)

        try:
            url = self._client.generate_presigned_url(
                ClientMethod="put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": s3_key,
                },
                ExpiresIn=expires_in,
                HttpMethod="PUT",
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error(f"Failed to generate presigned URL for '{filename}': {exc}")
            raise RuntimeError(
                f"Could not generate upload URL: {exc}"
            ) from exc

        result = PresignedUploadURL(
            presigned_url=url,
            s3_key=s3_key,
            document_id=document_id,
            filename=filename,
            expires_in=expires_in,
        )
        logger.info(
            f"Presigned PUT URL generated | key={s3_key} | "
            f"expires_in={expires_in}s"
        )
        return result

    def download_document(self, s3_key: str) -> bytes:
        """
        Download a document from S3 by key.

        Used by the Lambda document processor to fetch the raw file after
        the S3 event fires.

        Args:
            s3_key: Full S3 object key.

        Returns:
            Raw document bytes.

        Raises:
            RuntimeError: On S3 API failures.
        """
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=s3_key)
            content: bytes = response["Body"].read()
            logger.info(
                f"Downloaded '{s3_key}' ({len(content) / 1024:.1f} KB)"
            )
            return content
        except (BotoCoreError, ClientError) as exc:
            logger.error(f"S3 download failed for '{s3_key}': {exc}")
            raise RuntimeError(
                f"Failed to download document from S3: {exc}"
            ) from exc

    def parse_key(self, s3_key: str) -> S3KeyComponents:
        """
        Decode document_id and filename from a structured S3 key.

        Expected format:  {prefix}{document_id}/{filename}
        Example:          documents/3f7e1b2a/architecture-guide.pdf

        Used by the Lambda handler to reconstruct ingestion metadata
        without any external database lookup.

        Raises:
            ValueError: If the key format is unexpected.
        """
        relative = s3_key.removeprefix(self._prefix)
        parts = relative.split("/", maxsplit=1)

        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(
                f"S3 key '{s3_key}' does not match the expected format "
                f"'{self._prefix}{{document_id}}/{{filename}}'"
            )

        return S3KeyComponents(document_id=parts[0], filename=parts[1])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_key(self, document_id: str, filename: str) -> str:
        return f"{self._prefix}{document_id}/{filename}"

    @staticmethod
    def _resolve_content_type(filename: str) -> str:
        ext = Path(filename).suffix.lower()
        return _CONTENT_TYPE_MAP.get(ext, "application/octet-stream")

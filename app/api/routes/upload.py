"""
app/api/routes/upload.py

POST /upload  —  Ingest a document into the knowledge base.

Two modes, controlled by DOCUMENT_STORE_BACKEND in settings:

  local (default / development):
    File bytes → extract text → chunk → embed → FAISS / Pinecone (synchronous)
    Returns UploadResponse with chunks_indexed count.

  s3 (production):
    Generate a presigned S3 PUT URL → return it to the caller (async).
    The client PUTs the file directly to S3.
    An S3 event fires the Lambda document processor which does the
    extraction → chunking → embedding → Pinecone upsert asynchronously.
    Returns UploadJobResponse with presigned_url + document_id.

Error codes:
  400  Bad Request       — missing/invalid filename
  413  Payload Too Large — file exceeds MAX_UPLOAD_SIZE_MB  (local mode only)
  415  Unsupported Media — file type not in {.pdf, .txt}
  422  Unprocessable     — empty document or no extractable text (local mode only)
  500  Internal Error    — embedding, vector store, or S3 failure
"""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.api.dependencies import (
    get_embedding_service,
    get_s3_service,
    get_settings_dep,
    get_vector_store,
)
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.response import UploadJobResponse, UploadResponse
from app.services.document_processor import DocumentProcessor, SUPPORTED_EXTENSIONS
from app.services.embedding_service import EmbeddingService
from app.services.s3_service import S3Service
from app.services.vector_store import VectorStoreProtocol

router = APIRouter()
logger = get_logger(__name__)


@router.post(
    "/upload",
    status_code=200,
    summary="Ingest a document into the knowledge base",
    description=(
        "**local mode** (default): Synchronous — extracts, chunks, embeds, stores.\n\n"
        "**s3 mode**: Asynchronous — returns a presigned S3 PUT URL. "
        "Client uploads directly to S3; Lambda processes in background."
    ),
    responses={
        200: {"description": "Local mode — document processed synchronously"},
        202: {"description": "S3 mode — presigned URL returned, processing is async"},
        400: {"description": "Invalid or missing filename"},
        413: {"description": "File exceeds size limit (local mode)"},
        415: {"description": "Unsupported file type"},
        500: {"description": "Processing or S3 failure"},
    },
)
async def upload_document(
    file: Optional[UploadFile] = File(default=None, description="PDF or TXT file (local mode)"),
    filename: Optional[str] = Form(default=None, description="Filename for presigned URL (s3 mode)"),
    settings: Settings = Depends(get_settings_dep),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    vector_store: VectorStoreProtocol = Depends(get_vector_store),
    s3_service: Optional[S3Service] = Depends(get_s3_service),
):
    """
    Route to either mode based on DOCUMENT_STORE_BACKEND.

    S3 mode:   POST /upload  with form field `filename=architecture-guide.pdf`
               Returns 202 + presigned PUT URL.

    Local mode: POST /upload  with multipart `file=@doc.pdf`
               Returns 200 + chunks_indexed.
    """
    if settings.DOCUMENT_STORE_BACKEND == "s3":
        return await _handle_s3_upload(filename, s3_service)
    return await _handle_local_upload(file, settings, embedding_service, vector_store)


# ---------------------------------------------------------------------------
# S3 mode — presigned URL path
# ---------------------------------------------------------------------------

async def _handle_s3_upload(
    filename: Optional[str],
    s3_service: Optional[S3Service],
) -> UploadJobResponse:
    """
    Generate a presigned S3 PUT URL.

    The caller should:
      1. Receive this response (202).
      2. HTTP PUT the raw file bytes to `presigned_url`
         with `Content-Type` matching the file extension.
      3. S3 fires a Lambda event → document is processed asynchronously.
    """
    if not filename:
        raise HTTPException(
            status_code=400,
            detail=(
                "S3 mode requires a `filename` form field "
                "(e.g. filename=architecture-guide.pdf). "
                "No file bytes should be sent to this endpoint."
            ),
        )

    if s3_service is None:
        raise HTTPException(
            status_code=500,
            detail="S3Service is not initialised. Check AWS_BUCKET_NAME in settings.",
        )

    _validate_filename(filename)

    document_id = DocumentProcessor.generate_document_id()

    try:
        result = s3_service.generate_upload_url(
            filename=filename,
            document_id=document_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info(
        f"Presigned URL issued | doc_id={document_id} | file='{filename}' | "
        f"key={result.s3_key}"
    )

    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=202,
        content=UploadJobResponse(
            document_id=document_id,
            filename=filename,
            presigned_url=result.presigned_url,
            s3_key=result.s3_key,
            expires_in=result.expires_in,
            status="pending_upload",
            message=(
                f"PUT the file bytes to presigned_url with "
                f"Content-Type: {_content_type_hint(filename)}. "
                "Lambda will process and embed the document automatically."
            ),
        ).model_dump(),
    )


# ---------------------------------------------------------------------------
# Local mode — synchronous in-process path
# ---------------------------------------------------------------------------

async def _handle_local_upload(
    file: Optional[UploadFile],
    settings: Settings,
    embedding_service: EmbeddingService,
    vector_store: VectorStoreProtocol,
) -> UploadResponse:
    """
    Synchronous pipeline: extract → chunk → embed → store.
    Used for local development and single-node deployments.
    """
    if file is None:
        raise HTTPException(
            status_code=400,
            detail="Local mode requires a `file` multipart upload.",
        )

    _validate_filename(file.filename)

    file_content = await file.read()
    _validate_file_size(file_content, file.filename, settings.MAX_UPLOAD_SIZE_MB)

    logger.info(
        f"Local upload: '{file.filename}' ({len(file_content) / 1024:.1f} KB)"
    )

    processor = DocumentProcessor(settings=settings)
    document_id = DocumentProcessor.generate_document_id()

    try:
        raw_text = processor.extract_text(file_content, file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc))

    if not raw_text.strip():
        raise HTTPException(
            status_code=422,
            detail=(
                f"No extractable text in '{file.filename}'. "
                "The file may be empty, scanned (image-only PDF), or corrupted."
            ),
        )

    chunks = processor.chunk_text(
        text=raw_text,
        document_id=document_id,
        source_filename=file.filename,
    )
    if not chunks:
        raise HTTPException(
            status_code=422,
            detail=f"Could not produce any chunks from '{file.filename}'.",
        )

    try:
        embeddings = embedding_service.embed_texts([c.text for c in chunks])
        vector_store.add_chunks(chunks, embeddings)
    except Exception as exc:
        logger.error(f"Embed/store failed for '{file.filename}': {exc}")
        raise HTTPException(status_code=500, detail=f"Processing failed: {exc}")

    logger.info(
        f"Local ingestion complete | doc_id={document_id} | "
        f"file='{file.filename}' | chunks={len(chunks)}"
    )
    return UploadResponse(
        document_id=document_id,
        filename=file.filename,
        chunks_indexed=len(chunks),
        message=f"Document '{file.filename}' ingested with {len(chunks)} chunks.",
    )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_filename(filename: Optional[str]) -> None:
    if not filename:
        raise HTTPException(status_code=400, detail="Filename is required.")
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported file type '{ext}'. "
                f"Accepted: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            ),
        )


def _validate_file_size(content: bytes, filename: str, max_mb: int) -> None:
    size_mb = len(content) / (1024 * 1024)
    if size_mb > max_mb:
        raise HTTPException(
            status_code=413,
            detail=f"'{filename}' is {size_mb:.1f} MB, limit is {max_mb} MB.",
        )


def _content_type_hint(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return {"pdf": "application/pdf", ".txt": "text/plain"}.get(ext, "application/octet-stream")

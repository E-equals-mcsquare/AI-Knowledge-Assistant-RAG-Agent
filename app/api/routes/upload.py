"""
app/api/routes/upload.py

POST /upload  —  Ingest a document into the knowledge base.

Pipeline:
  1. Validate file type (PDF / TXT) and size
  2. Extract raw text
  3. Chunk text with sliding-window overlap
  4. Generate embeddings for each chunk (batched)
  5. Add vectors + metadata to FAISS
  6. Return document_id, filename, and chunk count

Error handling:
  - 400  Bad Request       — missing/invalid filename
  - 413  Payload Too Large — file exceeds MAX_UPLOAD_SIZE_MB
  - 415  Unsupported Media — file type not in {.pdf, .txt}
  - 422  Unprocessable     — file is empty or yields no text/chunks
  - 500  Internal Error    — OpenAI API failure or FAISS write failure
"""

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.dependencies import (
    get_embedding_service,
    get_settings_dep,
    get_vector_store,
)
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.response import UploadResponse
from app.services.document_processor import DocumentProcessor, SUPPORTED_EXTENSIONS
from app.services.embedding_service import EmbeddingService
from app.services.vector_store import VectorStoreService

router = APIRouter()
logger = get_logger(__name__)


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=200,
    summary="Ingest a document into the knowledge base",
    description=(
        "Upload a PDF or TXT file. "
        "The document is extracted, chunked, embedded, and stored in the "
        "FAISS vector index for later retrieval."
    ),
    responses={
        400: {"description": "Invalid or missing filename"},
        413: {"description": "File exceeds size limit"},
        415: {"description": "Unsupported file type"},
        422: {"description": "Empty document or no extractable text"},
        500: {"description": "Embedding or vector store failure"},
    },
)
async def upload_document(
    file: UploadFile = File(..., description="PDF or TXT document to ingest"),
    settings: Settings = Depends(get_settings_dep),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    vector_store: VectorStoreService = Depends(get_vector_store),
) -> UploadResponse:
    """
    Ingest a document into the knowledge base.

    Steps:
    1. Validate file name and extension
    2. Read and validate file size
    3. Extract raw text (PDF or TXT)
    4. Chunk text with configured overlap
    5. Generate embeddings for all chunks
    6. Store vectors + metadata in FAISS
    7. Return ingestion summary
    """
    _validate_filename(file.filename)

    file_content = await file.read()
    _validate_file_size(file_content, file.filename, settings.MAX_UPLOAD_SIZE_MB)

    size_kb = len(file_content) / 1024
    logger.info(
        f"Upload received: '{file.filename}' ({size_kb:.1f} KB) | "
        f"content_type={file.content_type}"
    )

    processor = DocumentProcessor(settings=settings)
    document_id = DocumentProcessor.generate_document_id()

    # --- Text extraction ---
    try:
        raw_text = processor.extract_text(file_content, file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc))

    if not raw_text.strip():
        raise HTTPException(
            status_code=422,
            detail=(
                f"No extractable text found in '{file.filename}'. "
                "The file may be empty, image-based (scanned PDF), or corrupted."
            ),
        )

    # --- Chunking ---
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

    # --- Embedding + storage ---
    try:
        chunk_texts = [c.text for c in chunks]
        embeddings = embedding_service.embed_texts(chunk_texts)
        vector_store.add_chunks(chunks, embeddings)
    except Exception as exc:
        logger.error(
            f"Failed to embed/store '{file.filename}' (doc_id={document_id}): {exc}"
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process document embeddings: {str(exc)}",
        )

    logger.info(
        f"Ingestion complete | doc_id={document_id} | "
        f"file='{file.filename}' | chunks={len(chunks)}"
    )

    return UploadResponse(
        document_id=document_id,
        filename=file.filename,
        chunks_indexed=len(chunks),
        message=(
            f"Document '{file.filename}' successfully ingested "
            f"with {len(chunks)} chunks."
        ),
    )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_filename(filename: str | None) -> None:
    """Raise 400 if the filename is missing or has an unsupported extension."""
    if not filename:
        raise HTTPException(status_code=400, detail="Uploaded file has no filename.")

    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported file type '{ext}'. "
                f"Accepted: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            ),
        )


def _validate_file_size(
    content: bytes,
    filename: str,
    max_mb: int,
) -> None:
    """Raise 413 if the file content exceeds the configured size limit."""
    size_mb = len(content) / (1024 * 1024)
    if size_mb > max_mb:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File '{filename}' is {size_mb:.1f} MB, "
                f"which exceeds the {max_mb} MB upload limit."
            ),
        )

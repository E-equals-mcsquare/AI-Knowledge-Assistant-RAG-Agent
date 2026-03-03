"""
app/services/document_processor.py

Handles all document ingestion concerns:
  1. Text extraction  — PDF (via pdfplumber) and plain TXT
  2. Text cleaning    — normalise whitespace, strip noise
  3. Chunking         — sliding-window over words with configurable
                        size and overlap; preserves word boundaries

Design notes:
- Stateless service: no I/O side effects, easy to unit-test
- pdfplumber chosen over PyPDF2/pypdf for better text-layer accuracy
- Character-based chunk_size/overlap are converted to word counts
  (avg English word ≈ 5 chars + 1 space = 6 chars) to avoid splitting
  mid-word and to keep embedding quality high

Future:
- Add DOCX support via python-docx
- Add OCR fallback (pytesseract) for scanned PDFs
- Move to a token-based splitter (tiktoken) once token budgets matter
"""

import io
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List

import pdfplumber

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".txt"}
# Approximate chars-per-word used to convert character budgets → word counts
_CHARS_PER_WORD = 6


@dataclass
class TextChunk:
    """A single chunk of text derived from an ingested document."""

    text: str
    chunk_id: int          # zero-based index within the document
    document_id: str       # UUID of the parent document
    source_filename: str   # original upload filename


class DocumentProcessor:
    """
    Extracts and chunks text from uploaded documents.

    Instantiated per-request (lightweight) — all state lives in settings.
    """

    def __init__(self, settings: Settings) -> None:
        self.chunk_size: int = settings.CHUNK_SIZE
        self.chunk_overlap: int = settings.CHUNK_OVERLAP

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract_text(self, file_content: bytes, filename: str) -> str:
        """
        Extract raw text from file content bytes.

        Args:
            file_content: Raw bytes of the uploaded file.
            filename:     Original filename (used to determine format).

        Returns:
            Extracted text as a single string.

        Raises:
            ValueError: If the file extension is not supported.
        """
        ext = Path(filename).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"Unsupported file type '{ext}'. "
                f"Supported formats: {', '.join(SUPPORTED_EXTENSIONS)}"
            )

        if ext == ".pdf":
            return self._extract_pdf(file_content)
        return self._extract_txt(file_content)

    def chunk_text(
        self,
        text: str,
        document_id: str,
        source_filename: str,
    ) -> List[TextChunk]:
        """
        Split text into overlapping chunks preserving word boundaries.

        Args:
            text:            Cleaned document text.
            document_id:     UUID of the parent document.
            source_filename: Original filename for metadata.

        Returns:
            Ordered list of TextChunk objects.
        """
        text = self._clean_text(text)
        raw_chunks = self._sliding_window(text)

        chunks = [
            TextChunk(
                text=chunk_text,
                chunk_id=idx,
                document_id=document_id,
                source_filename=source_filename,
            )
            for idx, chunk_text in enumerate(raw_chunks)
        ]

        logger.info(
            f"Chunked '{source_filename}' → {len(chunks)} chunks "
            f"(size≈{self.chunk_size} chars, overlap≈{self.chunk_overlap} chars)"
        )
        return chunks

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_pdf(self, file_content: bytes) -> str:
        """Extract text from PDF bytes using pdfplumber."""
        text_parts: List[str] = []
        num_pages = 0

        with pdfplumber.open(io.BytesIO(file_content)) as pdf:
            num_pages = len(pdf.pages)
            for page_num, page in enumerate(pdf.pages, start=1):
                page_text = page.extract_text()
                if page_text and page_text.strip():
                    text_parts.append(page_text.strip())
                    logger.debug(f"  Extracted page {page_num}/{num_pages}")

        full_text = "\n\n".join(text_parts)
        logger.info(
            f"PDF extraction: {len(full_text):,} chars from {num_pages} pages "
            f"({len(text_parts)} non-empty)"
        )
        return full_text

    def _extract_txt(self, file_content: bytes) -> str:
        """Decode and return plain text, trying UTF-8 then Latin-1."""
        for encoding in ("utf-8", "latin-1"):
            try:
                text = file_content.decode(encoding)
                logger.info(
                    f"TXT extraction ({encoding}): {len(text):,} chars"
                )
                return text
            except UnicodeDecodeError:
                continue
        raise ValueError(
            "Could not decode text file. "
            "Ensure the file is UTF-8 or Latin-1 encoded."
        )

    # ------------------------------------------------------------------
    # Chunking helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        """Normalise whitespace and remove common noise artifacts."""
        text = re.sub(r"\r\n|\r", "\n", text)          # normalise line endings
        text = re.sub(r"\n{3,}", "\n\n", text)          # collapse excess blank lines
        text = re.sub(r"[ \t]+", " ", text)             # collapse horizontal whitespace
        text = re.sub(r" *\n *", "\n", text)            # trim trailing spaces on lines
        return text.strip()

    def _sliding_window(self, text: str) -> List[str]:
        """
        Sliding-window chunker that operates on word tokens.

        Word-based splitting ensures chunks never break mid-word,
        which would degrade embedding quality. chunk_size and
        chunk_overlap (specified in characters) are converted to
        approximate word counts.
        """
        words = text.split()
        if not words:
            return []

        words_per_chunk = max(1, self.chunk_size // _CHARS_PER_WORD)
        overlap_words = max(0, self.chunk_overlap // _CHARS_PER_WORD)
        step = max(1, words_per_chunk - overlap_words)

        chunks: List[str] = []
        start = 0

        while start < len(words):
            end = min(start + words_per_chunk, len(words))
            chunk = " ".join(words[start:end])
            chunks.append(chunk)
            if end >= len(words):
                break
            start += step

        return chunks

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def generate_document_id() -> str:
        """Generate a new UUID4 to uniquely identify an ingested document."""
        return str(uuid.uuid4())

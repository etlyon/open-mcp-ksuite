"""In-memory document readers (copied from Hub tools for the kSuite split)."""
from __future__ import annotations

import mimetypes
import posixpath
import re
from typing import Any

MAX_PDF_PAGES = 75


def _clean_path(path: str) -> str:
    """Attachment names only (kSuite); keep a stable basename for format detection."""
    return posixpath.basename(path.replace("\\", "/").strip()) or path.strip()



# --- Document extraction, in memory (logic carried over) ----------------------


def _normalize_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r" +", " ", line).strip() for line in text.splitlines()]
    compact: list[str] = []
    blank = False
    for line in lines:
        if line:
            compact.append(line)
            blank = False
        elif compact and not blank:
            compact.append("")
            blank = True
    return "\n".join(compact).strip()


def _extract_pdf(data: bytes) -> tuple[str, dict[str, Any], list[str]]:
    from open_connectors.pdf_text import PdfTextError, extract_pdf_text

    try:
        return extract_pdf_text(data)
    except PdfTextError as exc:
        raise ValueError(str(exc)) from None


def _extract_docx(data: bytes, max_chars: int) -> tuple[str, dict[str, Any], list[str]]:
    from open_connectors.pdf_text import OfficeTextError, extract_office_text

    try:
        return extract_office_text(data, "docx", max_chars)
    except OfficeTextError as exc:
        raise ValueError(str(exc)) from None


def _extract_xlsx(data: bytes, max_chars: int) -> tuple[str, dict[str, Any], list[str]]:
    from open_connectors.pdf_text import OfficeTextError, extract_office_text

    try:
        return extract_office_text(data, "xlsx", max_chars)
    except OfficeTextError as exc:
        raise ValueError(str(exc)) from None


def _extract_pptx(data: bytes, max_chars: int) -> tuple[str, dict[str, Any], list[str]]:
    from open_connectors.pdf_text import OfficeTextError, extract_office_text

    try:
        return extract_office_text(data, "pptx", max_chars)
    except OfficeTextError as exc:
        raise ValueError(str(exc)) from None


def _extract_odf(data: bytes, max_chars: int) -> tuple[str, dict[str, Any], list[str]]:
    from open_connectors.pdf_text import OfficeTextError, extract_office_text

    try:
        return extract_office_text(data, "odf", max_chars)
    except OfficeTextError as exc:
        raise ValueError(str(exc)) from None


TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".css", ".js", ".py", ".sql", ".log", ".rtf",
}


def _extract_document(data: bytes, path: str, content_type: str | None, max_chars: int,
                      start_char: int = 0) -> dict[str, Any]:
    extension = posixpath.splitext(path)[1].lower()
    guessed_type = mimetypes.guess_type(path)[0]
    media_type = (content_type or guessed_type or "application/octet-stream").split(";", 1)[0].lower()
    warnings: list[str] = []
    stats: dict[str, Any] = {}

    if media_type.startswith("text/") or extension in TEXT_EXTENSIONS or media_type in {
        "application/json", "application/xml", "application/javascript",
    }:
        text = data.decode("utf-8-sig", errors="replace")
        document_format = "text"
    elif extension == ".pdf" or media_type == "application/pdf":
        text, stats, warnings = _extract_pdf(data)
        document_format = "pdf"
    elif extension in {".docx", ".dotx", ".docm"} or "wordprocessingml" in media_type:
        text, stats, warnings = _extract_docx(data, start_char + max_chars)
        document_format = "docx"
    elif extension in {".xlsx", ".xlsm", ".xltx"} or "spreadsheetml" in media_type:
        text, stats, warnings = _extract_xlsx(data, start_char + max_chars)
        document_format = "xlsx"
    elif extension in {".pptx", ".potx", ".ppsx"} or "presentationml" in media_type:
        text, stats, warnings = _extract_pptx(data, start_char + max_chars)
        document_format = "pptx"
    elif extension in {".odt", ".ods", ".odp", ".ott", ".ots", ".otp"} or "opendocument" in media_type:
        text, stats, warnings = _extract_odf(data, start_char + max_chars)
        document_format = "opendocument"
    else:
        raise ValueError(
            f"Unsupported document format '{extension or media_type}'. "
            "Supported formats include text, PDF, DOCX, XLSX, PPTX, and OpenDocument."
        )

    text = _normalize_text(text)
    total_characters = len(text)
    end_char = min(total_characters, start_char + max_chars)
    chunk = text[start_char:end_char]
    extraction_truncated = bool(stats.get("extraction_truncated"))
    truncated = end_char < total_characters or extraction_truncated
    next_start_char = end_char if truncated else None
    if start_char >= total_characters and total_characters:
        warnings.append(f"start_char {start_char} is beyond the extracted text length {total_characters}.")
    elif truncated:
        warnings.append(f"Returned characters {start_char}-{end_char}; continue with start_char={end_char}.")
    return {
        "path": _clean_path(path),
        "format": document_format,
        "content_type": media_type,
        "source_bytes": len(data),
        "characters": len(chunk),
        "total_characters_extracted": total_characters,
        "start_char": start_char,
        "end_char": end_char,
        "next_start_char": next_start_char,
        "truncated": truncated,
        "warnings": warnings,
        "stats": stats,
        "text": chunk,
    }

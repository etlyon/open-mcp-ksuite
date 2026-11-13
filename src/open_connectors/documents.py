"""In-memory document readers (copied from Hub tools for the kSuite split)."""
from __future__ import annotations

import mimetypes
import posixpath
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Any
from zipfile import BadZipFile, ZipFile

MAX_PDF_PAGES = 75
MAX_OFFICE_UNCOMPRESSED_BYTES = 200 * 1024 * 1024


def _xml(content: bytes) -> ET.Element:
    """Parse an ODF content.xml part; entity tricks are refused by ElementTree defaults here."""
    return ET.fromstring(content)


def _clean_path(path: str) -> str:
    """Attachment names only (kSuite); keep a stable basename for format detection."""
    return posixpath.basename(path.replace("\\", "/").strip()) or path.strip()



# --- Document extraction, in memory (logic carried over) ----------------------


def _validate_office_package(data: bytes) -> None:
    try:
        with ZipFile(BytesIO(data)) as archive:
            total = sum(item.file_size for item in archive.infolist())
    except BadZipFile as exc:
        raise ValueError("The office document is not a valid ZIP-based package.") from exc
    if total > MAX_OFFICE_UNCOMPRESSED_BYTES:
        raise ValueError("The expanded office document exceeds the configured safety limit.")


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
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError("The PDF is encrypted and cannot be read without a password.") from exc
    total_pages = len(reader.pages)
    page_limit = min(total_pages, MAX_PDF_PAGES)
    parts: list[str] = []
    warnings: list[str] = []
    for index in range(page_limit):
        try:
            page_text = reader.pages[index].extract_text() or ""
        except Exception:
            warnings.append(f"Page {index + 1} could not be extracted.")
            continue
        if page_text.strip():
            parts.append(f"[Page {index + 1}]\n{page_text}")
    if total_pages > page_limit:
        warnings.append(f"Only the first {page_limit} of {total_pages} pages were processed.")
    if not parts:
        warnings.append("No embedded text was found; this document may require OCR.")
    return "\n\n".join(parts), {"pages_total": total_pages, "pages_processed": page_limit}, warnings


def _extract_docx(data: bytes) -> tuple[str, dict[str, Any], list[str]]:
    from docx import Document

    _validate_office_package(data)
    document = Document(BytesIO(data))
    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    table_rows = 0
    for table in document.tables:
        for row in table.rows:
            values = [cell.text.strip() for cell in row.cells]
            if any(values):
                parts.append("\t".join(values))
                table_rows += 1
    return "\n".join(parts), {"paragraphs": len(document.paragraphs), "table_rows": table_rows}, []


def _extract_xlsx(data: bytes, max_chars: int) -> tuple[str, dict[str, Any], list[str]]:
    from openpyxl import load_workbook

    _validate_office_package(data)
    workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    sheet_count = len(workbook.sheetnames)
    parts: list[str] = []
    cells_read = 0
    truncated = False
    try:
        for worksheet in workbook.worksheets:
            parts.append(f"[Sheet: {worksheet.title}]")
            for row in worksheet.iter_rows(values_only=True):
                values = ["" if value is None else str(value) for value in row]
                while values and values[-1] == "":
                    values.pop()
                if not values:
                    continue
                parts.append("\t".join(values))
                cells_read += len(values)
                if sum(len(part) + 1 for part in parts) > max_chars * 2:
                    truncated = True
                    break
            if truncated:
                break
    finally:
        workbook.close()
    warnings = ["Spreadsheet processing stopped after reaching the extraction limit."] if truncated else []
    return "\n".join(parts), {"sheets": sheet_count, "cells_read": cells_read, "extraction_truncated": truncated}, warnings


def _extract_pptx(data: bytes) -> tuple[str, dict[str, Any], list[str]]:
    from pptx import Presentation

    _validate_office_package(data)
    presentation = Presentation(BytesIO(data))
    parts: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        slide_parts: list[str] = []
        for shape in slide.shapes:
            text = getattr(shape, "text", "")
            if text and text.strip():
                slide_parts.append(text.strip())
        if slide_parts:
            parts.append(f"[Slide {index}]\n" + "\n".join(slide_parts))
    return "\n\n".join(parts), {"slides": len(presentation.slides)}, []


def _extract_odf(data: bytes) -> tuple[str, dict[str, Any], list[str]]:
    _validate_office_package(data)
    with ZipFile(BytesIO(data)) as archive:
        try:
            content = archive.read("content.xml")
        except KeyError as exc:
            raise ValueError("The OpenDocument package does not contain content.xml.") from exc
    root = _xml(content)
    paragraphs: list[str] = []
    for element in root.iter():
        if element.tag.endswith("}p") or element.tag.endswith("}h"):
            text = "".join(element.itertext()).strip()
            if text:
                paragraphs.append(text)
    return "\n".join(paragraphs), {"paragraphs": len(paragraphs)}, []


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
        text, stats, warnings = _extract_docx(data)
        document_format = "docx"
    elif extension in {".xlsx", ".xlsm", ".xltx"} or "spreadsheetml" in media_type:
        text, stats, warnings = _extract_xlsx(data, start_char + max_chars)
        document_format = "xlsx"
    elif extension in {".pptx", ".potx", ".ppsx"} or "presentationml" in media_type:
        text, stats, warnings = _extract_pptx(data)
        document_format = "pptx"
    elif extension in {".odt", ".ods", ".odp", ".ott", ".ots", ".otp"} or "opendocument" in media_type:
        text, stats, warnings = _extract_odf(data)
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

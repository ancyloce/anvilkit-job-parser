"""anvilkit-parse: the fixed Docling parser of a parser Job (DD-07 §2).

Reads the staged input, applies the guards, converts PDF, DOCX, HTML and
Markdown with Docling (pinned weights under DOCLING_ARTIFACTS_PATH, OCR,
table structure, remote services, external plugins and every resource
fetch disabled) and plain text with a line-preserving paragraph splitter,
then chunks with Docling's hierarchical chunker. Every chunk keeps its
locator (element, heading path, page, line range) and quality flags; text
that reads like an instruction to a model is flagged, never followed.

The result is written to /stage/out/result.json for the trusted stager.
A refused document is a verified outcome (verdict rejected, exit 0); a
non-zero exit is an infrastructure failure the owner may retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from . import contract, guards

ELEMENTS = {
    "title": "heading",
    "section_header": "heading",
    "list_item": "list",
    "table": "table",
    "code": "code",
    "caption": "caption",
}

INSTRUCTION_LIKE = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|rules)"
    r"|\b(system|developer)\s+prompt\b"
    r"|\byou\s+are\s+now\b"
    r"|<\|?(im_start|im_end|system)\|?>"
    r"|^\s*#{1,6}\s*(system|instructions?)\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

DOCLING_FORMATS = {
    "application/pdf": ("PDF", "source.pdf"),
    guards.DOCX: ("DOCX", "source.docx"),
    "text/html": ("HTML", "source.html"),
    "text/markdown": ("MD", "source.md"),
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split(text: str, limit: int) -> list[str]:
    """Splits text into pieces of at most limit characters, at whitespace where possible."""
    pieces: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind(" ", 0, limit)
        cut = cut if cut > limit // 2 else limit
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return [p for p in pieces if p]


class LineFinder:
    """Finds the 1-based line range of successive chunks in a Markdown source.

    Docling joins a chunk's items with newlines; the chunk's first and last
    lines are matched (whitespace and Markdown markers normalized) against
    the source lines at or after the previous chunk's end. A chunk that
    cannot be matched carries no line range rather than a guessed one.
    """

    def __init__(self, text: str) -> None:
        self.lines = [self._norm(line) for line in text.splitlines()]
        self.cursor = 0

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[\s#>*_`|\-]+", " ", s).strip().lower()

    @staticmethod
    def _matches(part: str, line: str) -> bool:
        return bool(line) and (part in line or line in part)

    def locate(self, chunk: str) -> tuple[int, int] | None:
        parts = [self._norm(x) for x in chunk.splitlines() if self._norm(x)]
        if not parts:
            return None
        head, tail = parts[0][:60], parts[-1][-60:]
        for i in range(self.cursor, len(self.lines)):
            if self._matches(head, self.lines[i]):
                for j in range(i, len(self.lines)):
                    if self._matches(tail, self.lines[j]):
                        self.cursor = j
                        return i + 1, j + 1
                self.cursor = i
                return i + 1, i + 1
        return None


class Builder:
    """Accumulates bounded chunks with their provenance."""

    def __init__(self, limits: contract.Limits) -> None:
        self.limits = limits
        self.chunks: list[dict[str, Any]] = []

    def add(self, text: str, element: str, headings: list[str], page: int | None, lines: tuple[int, int] | None) -> None:
        text = text.strip()
        if not text:
            return
        pieces = _split(text, self.limits.chunker["maxChunkChars"])
        for piece in pieces:
            if len(self.chunks) >= self.limits.maxChunks:
                raise guards.Rejected("CHUNK_LIMIT_EXCEEDED", f"more than {self.limits.maxChunks} chunks")
            flags = []
            if INSTRUCTION_LIKE.search(piece):
                flags.append("instruction_like")
            if len(pieces) > 1:
                flags.append("split_block")
            if element in ("table", "code"):
                flags.append(element)
            if len(piece) < 16:
                flags.append("low_text")
            locator: dict[str, Any] = {
                "element": element,
                "headingPath": [h.strip()[:256] for h in headings if h.strip()][:16],
            }
            if page is not None:
                locator["page"] = page
            if lines is not None:
                locator["lineStart"], locator["lineEnd"] = lines
            self.chunks.append(
                {
                    "ordinal": len(self.chunks),
                    "text": piece,
                    "contentDigest": "sha256:" + hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                    "locator": locator,
                    "qualityFlags": flags,
                }
            )


def plain_text(data: bytes, builder: Builder) -> int:
    """Paragraphs separated by blank lines, each with its exact line range."""
    lines = data.decode("utf-8").splitlines()
    para: list[str] = []
    start = 0
    for i, line in enumerate(lines + [""]):
        if line.strip():
            if not para:
                start = i
            para.append(line)
        elif para:
            builder.add("\n".join(para), "text", [], None, (start + 1, i))
            para = []
    return 0


def docling(data: bytes, media_type: str, limits: contract.Limits, builder: Builder) -> int:
    from docling.datamodel.backend_options import HTMLBackendOptions, MarkdownBackendOptions
    from docling.datamodel.base_models import ConversionStatus, DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import (
        DocumentConverter,
        HTMLFormatOption,
        MarkdownFormatOption,
        PdfFormatOption,
        WordFormatOption,
    )
    from docling_core.transforms.chunker.hierarchical_chunker import HierarchicalChunker

    artifacts = Path(os.environ.get("DOCLING_ARTIFACTS_PATH", "/opt/docling-models"))
    pdf = PdfPipelineOptions(
        artifacts_path=artifacts,
        do_ocr=False,
        do_table_structure=False,
        do_code_enrichment=False,
        do_formula_enrichment=False,
        do_picture_classification=False,
        do_picture_description=False,
        generate_page_images=False,
        generate_picture_images=False,
        enable_remote_services=False,
        allow_external_plugins=False,
    )
    no_fetch = {"enable_remote_fetch": False, "enable_local_fetch": False}
    converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.HTML, InputFormat.MD],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf),
            InputFormat.DOCX: WordFormatOption(),
            InputFormat.HTML: HTMLFormatOption(backend_options=HTMLBackendOptions(**no_fetch, fetch_images=False)),
            InputFormat.MD: MarkdownFormatOption(backend_options=MarkdownBackendOptions(**no_fetch, fetch_images=False)),
        },
    )
    _, name = DOCLING_FORMATS[media_type]
    result = converter.convert(
        DocumentStream(name=name, stream=BytesIO(data)),
        max_num_pages=limits.maxPages,
        max_file_size=limits.maxInputBytes,
        raises_on_error=False,
    )
    if result.status != ConversionStatus.SUCCESS:
        raise guards.Rejected("MALFORMED_DOCUMENT", f"conversion {result.status.value}")
    doc = result.document
    finder = LineFinder(data.decode("utf-8")) if media_type == "text/markdown" else None
    paged = media_type == "application/pdf"
    for ch in HierarchicalChunker().chunk(doc):
        items = ch.meta.doc_items or []
        label = str(getattr(items[0].label, "value", items[0].label)) if items else "text"
        pages = [p.page_no for it in items for p in (it.prov or [])]
        page = min(pages) if paged and pages else None
        lines = finder.locate(ch.text) if finder else None
        builder.add(ch.text, ELEMENTS.get(label, "text"), list(ch.meta.headings or []), page, lines)
    return len(doc.pages) if paged else 0


def run(schema: contract.Schema, env: dict[str, str] | None = None, input_path: Path | None = None) -> dict[str, Any]:
    envelope, limits = contract.load(schema, env)
    source = input_path or contract.INPUT_PATH
    inp = envelope.input
    base: dict[str, Any] = {
        "schemaVersion": 1,
        "launchKey": envelope.launch_key,
        "profileId": envelope.raw["profileId"],
        "profileRevision": envelope.raw["profileRevision"],
        "input": dict(inp),
        "parser": {"name": limits.parserName, "version": limits.parserVersion},
        "chunker": {"chunkerId": limits.chunker["chunkerId"], "revision": limits.chunker["revision"]},
    }
    builder = Builder(limits)
    try:
        size = source.stat().st_size
        if size > limits.maxInputBytes:
            raise guards.Rejected("INPUT_TOO_LARGE", f"{size} bytes")
        data = source.read_bytes()
        guards.check_input(data, inp["digest"], int(inp["sizeBytes"]), limits.maxInputBytes)
        media = inp["mediaType"]
        guards.check_media_type(data, media, limits.mediaTypes)
        if media == guards.DOCX:
            guards.check_archive(
                data, limits.maxArchiveEntries, limits.maxDecompressedBytes, limits.maxDecompressionRatio
            )
        if media == "application/pdf":
            guards.check_pdf(data, limits.maxPages)
        pages = plain_text(data, builder) if media == "text/plain" else docling(data, media, limits, builder)
        result = {**base, "verdict": "parsed", "pageCount": pages, "chunks": builder.chunks, "completedAt": _now()}
        if len(encode(result)) > limits.maxOutputBytes:
            raise guards.Rejected("OUTPUT_TOO_LARGE", f"over {limits.maxOutputBytes} bytes")
    except guards.Rejected as r:
        result = {**base, "verdict": "rejected", "failureCode": r.code, "pageCount": 0, "chunks": [], "completedAt": _now()}
    schema.validate("#/$defs/parseResult", result)
    return result


def encode(result: dict[str, Any]) -> bytes:
    return json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def main() -> int:
    try:
        schema = contract.Schema()
        result = run(schema)
    except contract.ContractError as err:
        sys.stderr.write(f"parser contract: {err}\n")
        return 2
    contract.RESULT_PATH.write_bytes(encode(result))
    sys.stderr.write(f"parser verdict: {result['verdict']} {result.get('failureCode', '')} chunks={len(result['chunks'])}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

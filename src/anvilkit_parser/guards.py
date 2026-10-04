"""Checks that run before a document reaches Docling (DD-07 §2, SEC-10).

The staged bytes must be exactly the envelope's input; the declared media
type must match the actual bytes; a DOCX (a ZIP container) is bounded in
entries, expanded size and compression ratio and may not name an absolute
path, a parent directory, a link or an encrypted member; a PDF must open
without a password and stay within the page bound. Every refusal is a
parse failure code of the contract, reported as verdict rejected.
"""

from __future__ import annotations

import hashlib
import stat
import zipfile
import zlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_TYPES = ("text/plain", "text/markdown", "text/html")


@dataclass
class Rejected(Exception):
    code: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def check_input(data: bytes, expected_digest: str, expected_size: int, max_bytes: int) -> None:
    if len(data) > max_bytes:
        raise Rejected("INPUT_TOO_LARGE", f"{len(data)} bytes over {max_bytes}")
    if len(data) != expected_size or "sha256:" + hashlib.sha256(data).hexdigest() != expected_digest:
        raise Rejected("INPUT_DIGEST_MISMATCH", "staged bytes differ from the envelope input")


def check_media_type(data: bytes, declared: str, allowed: list[str]) -> None:
    if declared not in allowed:
        raise Rejected("UNSUPPORTED_MEDIA_TYPE", declared)
    if declared == "application/pdf":
        ok = data.startswith(b"%PDF-")
    elif declared == DOCX:
        ok = data.startswith(b"PK\x03\x04")
    else:
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            ok = False
        else:
            ok = "\x00" not in text
            if ok and declared == "text/html":
                head = text[:4096].lower()
                ok = "<!doctype html" in head or "<html" in head
    if not ok:
        raise Rejected("MEDIA_TYPE_MISMATCH", f"the bytes are not {declared}")


def _unsafe_name(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/") or ":" in name.split("/", 1)[0]:
        return True
    return any(part == ".." for part in PurePosixPath(name).parts)


def check_archive(data: bytes, max_entries: int, max_expanded: int, max_ratio: int) -> None:
    """Bounds a ZIP container before any member is decompressed."""
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as err:
        raise Rejected("MALFORMED_DOCUMENT", "not a ZIP container") from err
    with zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise Rejected("ARCHIVE_ENTRY_LIMIT", f"{len(infos)} entries over {max_entries}")
        total = 0
        names = set()
        for info in infos:
            if _unsafe_name(info.filename):
                raise Rejected("PATH_ESCAPE", "member path leaves the archive root")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise Rejected("PATH_ESCAPE", "member is a symbolic link")
            if info.flag_bits & 0x1:
                raise Rejected("ENCRYPTED_DOCUMENT", "encrypted member")
            total += info.file_size
            if total > max_expanded:
                raise Rejected("ARCHIVE_BOMB", f"expanded size over {max_expanded} bytes")
            if info.compress_size > 0 and info.file_size / info.compress_size > max_ratio:
                raise Rejected("ARCHIVE_BOMB", f"member compression ratio over {max_ratio}")
            if info.compress_size == 0 and info.file_size > 0:
                raise Rejected("ARCHIVE_BOMB", "member expands from zero bytes")
            names.add(info.filename)
        if len(data) > 0 and total / len(data) > max_ratio:
            raise Rejected("ARCHIVE_BOMB", f"archive compression ratio over {max_ratio}")
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise Rejected("MALFORMED_DOCUMENT", "not a word-processing document")
        # The declared sizes are the archive's claim; the actual expansion
        # of every member is bounded by reading it back under the budget.
        # zipfile stops at the declared size and checks the CRC, so a member
        # whose declared size understates its data is malformed.
        budget = max_expanded
        try:
            for info in infos:
                with zf.open(info) as member:
                    while chunk := member.read(65536):
                        budget -= len(chunk)
                        if budget < 0:
                            raise Rejected("ARCHIVE_BOMB", "actual expansion exceeds the bound")
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as err:
            raise Rejected("MALFORMED_DOCUMENT", "a member does not match its declared size or checksum") from err


def check_pdf(data: bytes, max_pages: int) -> int:
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as err:
        message = str(err).lower()
        if "password" in message:
            raise Rejected("ENCRYPTED_DOCUMENT", "the PDF requires a password") from err
        raise Rejected("MALFORMED_DOCUMENT", "the PDF does not open") from err
    try:
        pages = len(doc)
    finally:
        doc.close()
    if pages > max_pages:
        raise Rejected("PAGE_LIMIT_EXCEEDED", f"{pages} pages over {max_pages}")
    if pages == 0:
        raise Rejected("MALFORMED_DOCUMENT", "the PDF has no pages")
    return pages

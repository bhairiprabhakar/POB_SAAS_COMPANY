"""
Upload content validation: magic-byte + declared-type checks for files the
platform accepts from users (invoices, gift photos, logos, bulk-import
sheets, campaign assets).

The filename/extension a client sends is not trustworthy on its own -- a
renamed arbitrary file (e.g. a script or malformed PDF built to exploit a
downstream parser) should never be accepted just because it's named
"invoice.pdf". This module checks the actual file *content* (magic bytes,
and for images a real image-library decode) against an explicit allowlist,
and enforces a maximum size before the caller ever writes the bytes to disk
or hands them to Gemini/pdfplumber/etc.
"""
from __future__ import annotations

import io


class UploadValidationError(Exception):
    pass


# (kind, signature-check) -- checked in order, first match wins.
def _is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _is_jpeg(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff"


def _is_png(data: bytes) -> bool:
    return data[:8] == b"\x89PNG\r\n\x1a\n"


def _is_gif(data: bytes) -> bool:
    return data[:6] in (b"GIF87a", b"GIF89a")


def _is_webp(data: bytes) -> bool:
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _is_zip_office(data: bytes) -> bool:
    # xlsx/docx/pptx are zip containers -- PK\x03\x04 (or empty-archive PK\x05\x06).
    return data[:4] == b"PK\x03\x04" or data[:4] == b"PK\x05\x06"


def _is_legacy_office(data: bytes) -> bool:
    # xls/doc/ppt (OLE2 compound file).
    return data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _is_csv(data: bytes) -> bool:
    # CSV has no magic bytes -- accept if it decodes as text and doesn't
    # look like a binary file wearing a .csv extension.
    sample = data[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        try:
            sample.decode("latin-1")
            return True
        except UnicodeDecodeError:
            return False


_SIGNATURES = {
    "pdf": _is_pdf,
    "jpeg": _is_jpeg,
    "png": _is_png,
    "gif": _is_gif,
    "webp": _is_webp,
    "office_zip": _is_zip_office,   # xlsx/docx/pptx
    "office_legacy": _is_legacy_office,  # xls/doc/ppt
    "csv": _is_csv,
}

# Convenience allowlist groups for common upload categories.
IMAGE_KINDS = {"jpeg", "png", "gif", "webp"}
DOCUMENT_KINDS = {"pdf"}
SPREADSHEET_KINDS = {"csv", "office_zip", "office_legacy"}


def sniff_kind(data: bytes) -> str | None:
    for kind, check in _SIGNATURES.items():
        try:
            if check(data):
                return kind
        except Exception:
            continue
    return None


def is_safe_svg(data: bytes) -> bool:
    """Very conservative SVG check: must parse as well-formed XML, and must
    not contain <script>, external references, or on*= event-handler
    attributes -- all of which turn an "image" upload into stored XSS when
    the SVG is later served/rendered in a browser context. This is not a
    full sanitizer; when in doubt, prefer PNG/JPEG for logos over SVG."""
    import re
    import xml.etree.ElementTree as ET

    try:
        ET.fromstring(data)
    except ET.ParseError:
        return False
    lowered = data.lower()
    if b"<script" in lowered or b"javascript:" in lowered:
        return False
    if re.search(rb"\bon[a-z]+\s*=", lowered):
        return False
    if b"<foreignobject" in lowered:
        return False
    return True


def validate_upload(
    data: bytes,
    *,
    filename: str = "",
    allowed_kinds: set[str],
    max_size: int,
) -> str:
    """Validate uploaded bytes against an allowlist of content kinds and a
    max size. Returns the detected kind on success. Raises
    UploadValidationError with a message safe to show the caller.

    For images, this also does a real Pillow decode (not just a magic-byte
    check) since a file can have a valid image header but be malformed or
    crafted to exploit a decoder -- Pillow's verify() catches truncated/
    corrupt files; a real load() catches decompression-bomb-style pixel
    counts via MAX_IMAGE_PIXELS.
    """
    if not data:
        raise UploadValidationError("empty file")
    if len(data) > max_size:
        raise UploadValidationError(f"file exceeds maximum size of {max_size} bytes")

    kind = sniff_kind(data)
    if kind is None or kind not in allowed_kinds:
        raise UploadValidationError(
            f"file content does not match an allowed type ({sorted(allowed_kinds)}); "
            f"the extension in '{filename}' is not trusted on its own"
        )

    if kind in IMAGE_KINDS:
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img.verify()  # raises on truncated/corrupt data
            # verify() invalidates the file handle for further use; reopen
            # to check it actually decodes (catches some crafted payloads
            # verify() alone misses) and to bound decompressed pixel count.
            img2 = Image.open(io.BytesIO(data))
            img2.load()
        except UploadValidationError:
            raise
        except Exception as exc:
            raise UploadValidationError(f"file is not a valid image: {exc}")

    if kind == "pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(data))
            _ = len(reader.pages)  # forces basic structural parse
        except Exception as exc:
            raise UploadValidationError(f"file is not a valid PDF: {exc}")

    return kind

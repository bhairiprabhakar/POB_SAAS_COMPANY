"""
Remote document ingestion -- lets a user import a document by URL instead of
a local file picker: S3/EC2-hosted links, Google Drive share links, or any
direct web link to a PDF/image. Merged home of the legacy app/ingestion.py;
logic is identical.

SECURITY NOTE (read before changing this file): this fetches a
user-supplied URL from the SERVER. That is a classic SSRF vector -- without
the checks below, a user could ask this server to fetch
"http://169.254.169.254/latest/meta-data/iam/security-credentials/..."
(the AWS/EC2 instance-metadata endpoint) or an internal admin panel on your
private network, and get the response back through the extraction pipeline.
Every check in `_validate_host_or_raise` exists to close one of those doors.
Do not remove them to "make a URL work" without understanding why they're there.

Deliberately NOT supported: file:// or any local-filesystem path/UNC share
typed into a URL field. A raw local path from a web form is a path-traversal /
arbitrary-file-read risk on the server, not a "download" at all -- if you
need to import a document already sitting on this server's disk, use the
existing local file upload instead.
"""
import ipaddress
import os
import re
import socket
import tempfile
from urllib.parse import urlparse, unquote, parse_qs

import requests

from .config import MAX_UPLOAD_SIZE

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}

_MAGIC_BYTES = {
    b"%PDF": ".pdf",
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
}

_GOOGLE_DRIVE_ID_RE = re.compile(r"/file/d/([a-zA-Z0-9_-]+)")


class RemoteFetchError(Exception):
    """Raised for any user-facing reason a remote document couldn't be
    imported -- message is safe to show directly to the end user."""


def _normalize_url(raw_url: str) -> str:
    """Rewrite known share-link formats (currently: Google Drive) into a
    direct-download URL. Anything else is passed through unchanged --
    generic web links and S3/EC2-hosted links are already direct."""
    parsed = urlparse(raw_url)
    host = (parsed.hostname or "").lower()

    if host in ("drive.google.com", "www.drive.google.com"):
        m = _GOOGLE_DRIVE_ID_RE.search(parsed.path)
        file_id = None
        if m:
            file_id = m.group(1)
        else:
            qs = parse_qs(parsed.query)
            if "id" in qs:
                file_id = qs["id"][0]
        if file_id:
            return f"https://drive.google.com/uc?export=download&id={file_id}"

    return raw_url


def _validate_host_or_raise(url: str) -> None:
    """Block SSRF-favourite targets: non-http(s) schemes, loopback, private
    (RFC1918), link-local (this is what 169.254.169.254 -- the AWS/EC2 and
    GCP metadata endpoint -- falls under), and multicast/reserved ranges."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RemoteFetchError(
            f"Unsupported link type '{parsed.scheme}://'. Only http:// and https:// links are supported."
        )
    host = parsed.hostname
    if not host:
        raise RemoteFetchError("That doesn't look like a valid URL.")

    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise RemoteFetchError(f"Could not resolve host '{host}'.")

    for family, _, _, _, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise RemoteFetchError(
                "This link points to a private/internal network address, which isn't allowed."
            )


def _guess_ext_from_headers_or_url(resp, url: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
    if m:
        name = unquote(m.group(1))
        ext = os.path.splitext(name)[1].lower()
        if ext in ALLOWED_EXTENSIONS:
            return ext
    path_ext = os.path.splitext(urlparse(url).path)[1].lower()
    if path_ext in ALLOWED_EXTENSIONS:
        return path_ext
    ct = (resp.headers.get("content-type") or "").lower()
    return {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png"}.get(ct, "")


def _original_name_from(resp, url: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
    if m:
        return unquote(m.group(1)).strip('"')
    base = os.path.basename(urlparse(url).path) or "document"
    return unquote(base)


def download_remote_document(raw_url: str):
    """
    Fetch a document from a URL (S3/EC2 link, Google Drive share link, or
    any direct web link). Returns (temp_path, ext, original_filename).
    Raises RemoteFetchError with a user-safe message on any failure.
    Caller is responsible for moving/removing the returned temp file.
    """
    if not raw_url or not raw_url.strip():
        raise RemoteFetchError("Please provide a URL.")
    raw_url = raw_url.strip()

    url = _normalize_url(raw_url)
    _validate_host_or_raise(url)

    try:
        resp = requests.get(
            url, stream=True, timeout=20, allow_redirects=True,
            headers={"User-Agent": "PharmaAnalyzer-DocumentImport/1.0"},
        )
    except requests.RequestException as e:
        raise RemoteFetchError(f"Could not fetch that link: {e}")

    # requests follows redirects itself; re-validate the FINAL host actually
    # reached, since a first host could redirect to an internal address.
    _validate_host_or_raise(resp.url)

    if resp.status_code != 200:
        resp.close()
        raise RemoteFetchError(f"The link returned HTTP {resp.status_code} -- check it's publicly accessible.")

    content_length = resp.headers.get("content-length")
    max_mb = MAX_UPLOAD_SIZE // (1024 * 1024)
    if content_length and int(content_length) > MAX_UPLOAD_SIZE:
        resp.close()
        raise RemoteFetchError(f"File is too large ({int(content_length)//1024//1024} MB). Max is {max_mb} MB.")

    ext = _guess_ext_from_headers_or_url(resp, url)
    original_name = _original_name_from(resp, url)

    fd, tmp_path = tempfile.mkstemp(suffix=ext or "")
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            first_chunk = True
            sniffed_ext = None
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_UPLOAD_SIZE:
                    raise RemoteFetchError(f"File exceeded the {max_mb} MB limit while downloading.")
                if first_chunk:
                    for magic, m_ext in _MAGIC_BYTES.items():
                        if chunk.startswith(magic):
                            sniffed_ext = m_ext
                            break
                    first_chunk = False
                out.write(chunk)
    except RemoteFetchError:
        os.remove(tmp_path)
        raise
    finally:
        resp.close()

    final_ext = sniffed_ext or ext
    if final_ext not in ALLOWED_EXTENSIONS:
        os.remove(tmp_path)
        raise RemoteFetchError(
            "That link doesn't point to a supported document type (PDF, JPG, or PNG)."
        )
    if not original_name.lower().endswith(final_ext):
        original_name = os.path.splitext(original_name)[0] + final_ext

    return tmp_path, final_ext, original_name
"""
OCR / extraction engine -- Google Gemini Flash 3.5 (File API), replacing the
old pdfplumber + PaddleOCR + custom-router pipeline entirely. Merged home of
the legacy app/ai/gemini_extraction.py; logic is identical, only the config /
DB sources changed (saas.config; usage logged to the platform control-plane
DB, see saas/ai/__init__.py).

Public entry point kept close to the original app.py:

    call_gemini_extraction(file_path, file_type, original_name, company_id=None, upload_id=None) -> dict

...so saas/extraction.py::call_ocr_extraction() and the /upload flow needed
only a couple of extra (optional, backward-compatible) keyword args added --
see saas/extraction.py.

WHAT'S NEW IN THIS VERSION (large-document handling + cost tracking):

1. PAGE-COUNT CHUNKING. Gemini 3.5 Flash's INPUT window is enormous (1M
   tokens) so page count alone rarely chokes it -- the real limit that
   matters here is the 65,536-token OUTPUT window. A 100+ page pharma
   distributor statement can have thousands of line items; the JSON
   describing all of them can exceed 65K output tokens, and the response
   comes back TRUNCATED mid-JSON with no recovery. Documents over
   GEMINI_MAX_PAGES_PER_CHUNK pages are now split into batches, each
   extracted separately, then merged into one combined result.

2. THE RIGHT CONCURRENCY TOOL FOR EACH PART. Splitting a PDF into chunk
   files (pypdf re-writing pages) is genuinely CPU-bound work, so that step
   uses a ProcessPoolExecutor (real multiprocessing, bypasses the GIL).
   Extracting each chunk via Gemini is network I/O -- the thread waits on a
   response, releasing the GIL for the whole wait -- so THAT step uses a
   ThreadPoolExecutor instead; spinning up separate OS processes for a
   network wait would only add process/IPC overhead with zero speed benefit.
   Using multiprocessing for the network calls too would not make them
   faster, only heavier.

3. ADAPTIVE RETRY ON TRUNCATION. If a chunk's JSON still fails to parse
   after normal retries, that's a signal the chunk itself was too dense
   (too many line items for one response) -- rather than giving up, it's
   split in half and each half is retried, recursively, down to a 1-page
   floor.

5. MODEL SELECTION (gemini-2.5-flash vs gemini-3.5-flash). Based on real
   accuracy testing (not a guess): gemini-3.5-flash handles scanned/image
   documents at ~100% accuracy; gemini-2.5-flash is ~5x cheaper on input and
   ~3.6x cheaper on output, and testing showed it performs just as well on
   PURE-TEXT PDFs specifically (a real extractable text layer, no scan/image
   content) -- so that's the only case it's used for. See _select_model()
   and _pdf_has_text_layer() below for the actual per-document detection;
   images always go to the visual model, and a PDF only gets the cheaper
   model if it demonstrably has real embedded text, not a guess based on
   file extension alone.
"""
import json
import mimetypes
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

from google import genai
from google.genai import types

from ..config import (
    GEMINI_MODEL,
    GEMINI_MAX_PAGES_PER_CHUNK, GEMINI_CHUNK_PARALLELISM,
)
from ..config import GOOGLE_API_KEY as GEMINI_API_KEY
from . import model_registry
from ..ai_cleanup import cleanup_orphaned_gemini_files  # noqa: F401  (public re-export)

_client = None


def _get_client():
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set")
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


EXTRACTION_PROMPT = """You are a document-extraction engine for pharmaceutical
distributor sales statements and invoices (Indian pharma trade).

Read the attached document and return ONLY a single JSON object -- no markdown
fences, no explanation, no leading/trailing text. If a field is not present in
the document, use an empty string "" (or 0 for numbers, [] for lists).

Return exactly this shape:

{
  "AgencyDetails": {
    "Name": "",        // stockist / agency / distributor name
    "GSTIN": "",
    "Address": ""
  },
  "ReportDetails": {
    "Company": "",     // pharma company / manufacturer name on the document
    "InvoiceNumber": "",
    "InvoiceDate": "",
    "FromDate": "",    // statement period start, DD/MM/YYYY if possible
    "ToDate": ""        // statement period end, DD/MM/YYYY if possible
  },
  "DocType": "STATEMENT",  // "STATEMENT" or "INVOICE"
  "Areas": [
    {
      "AreaName": "",
      "Stores": [
        {
          "StoreName": "",       // retailer / chemist / party name
          "StoreLocation": "",
          "DLNumber": "",
          "GSTNumber": "",
          "Items": [
            {
              "Description": "",   // brand / product name only (e.g. "PANTOLUP DSR") -- never include pack size or qty here
              "Manufacturer": "",
              "Pack": "",           // PACK / PACKING SIZE printed on the document -- the physical unit configuration,
                                    // e.g. "1X10", "10X10'S", "1*21GM", "500MG". This describes ONE UNIT of the product
                                    // (how many tablets per strip, grams per bottle, etc). It is usually found in a
                                    // column literally labeled "Pack", "Packing", or "Size" on the source document.
                                    // Pack is a fixed property of the product/brand and does NOT change based on how
                                    // many units were sold in this transaction.
              "BatchNo": "",
              "Expiry": "",
              "HSN": "",
              "Qty": 0,             // QUANTITY SOLD/BILLED in this transaction -- a plain count (e.g. 1, 2, 5, 12),
                                    // usually found in a column labeled "Qty", "Quantity", or "Q". This is HOW MANY
                                    // of the packs above were sold -- it is a completely different number from Pack
                                    // and must never be copied into the Pack field or vice versa.
              "MRP": 0,
              "Rate": 0,
              "Percent": 0,        // discount percent
              "GST": 0,
              "Amount": 0,         // final line amount
              "DocType": ""
            }
          ]
        }
      ]
    }
  ],
  "InvoiceTotal": 0,
  "NetTotal": 0,
  "ConfidenceScore": 0.0,
  "ValidationNotes": ""
}

Rules:
- Every numeric field must be a plain JSON number (no currency symbols, no commas).
- Dates should be normalised to DD/MM/YYYY where the source format is unambiguous.
- CRITICAL -- Pack vs Qty are NEVER the same value. Pack describes the product's
  fixed packing configuration (how many tablets/capsules/ml per strip/bottle,
  e.g. "1X10", "10X10'S", "1*21GM"); Qty is how many of that pack were sold in
  this specific line (a small integer like 1, 2, 5, 10, 12). They typically
  come from two different columns on the source document (often labelled
  something like "Pack"/"Packing" and "Qty"/"Quantity" respectively, sometimes
  adjacent to each other). Do not leave Pack blank and put its value in Qty,
  and do not put the Qty number into Pack. If a document genuinely has no
  separate packing-size column, leave Pack as "" rather than guessing or
  reusing the Qty value.
  Worked example: a line reading "PANTOLUP DSR   1X10   2   140.00   280.00"
  (product, pack, qty, rate, amount) must produce
  {"Description":"PANTOLUP DSR","Pack":"1X10","Qty":2,"Rate":140.00,"Amount":280.00}
  -- NOT {"Pack":"2", ...} and NOT {"Pack":"","Qty":"1X10", ...}.
- ALTERNATE LAYOUT -- MONTHLY PARTY SUMMARY (no individual brand/item lines):
  Some stockist statements list ONLY party names with monthly sales columns
  and a total, with NO per-brand/per-item breakdown at all -- for example a
  table with columns like "Party | Oct | Nov | Dec | Total Value" and rows
  like "A V MEDICOS FEROZEPU   5641   1605   741   7909.10" (party name,
  then one number per month, then a grand total for that party). This is a
  real, common document type -- do NOT return an empty Areas/Stores/Items
  list just because there's no brand-level detail to extract. Instead, for
  each party row, create ONE Store entry with exactly ONE synthetic Item:
    {"Description": "Monthly Sales Summary", "Pack": "", "Qty": 0,
     "Rate": 0, "Amount": <that party's Total Value column>}
  so the party name and its total value are still captured even though no
  individual products are listed. Put the party's own name (e.g.
  "A V MEDICOS FEROZEPU") in "StoreName" exactly as printed, including any
  location suffix. Use "AreaName": "General" if the document has no
  separate area/territory grouping in this layout.
- If the document lists multiple parties/retailers under one stockist statement,
  create one entry per party under "Stores".
- This document may be a PARTIAL EXCERPT of a larger statement (pages N-M of a
  bigger file). Extract only what appears in these pages -- do not invent
  totals or parties that aren't shown. If InvoiceTotal/NetTotal aren't visible
  in this excerpt, use 0; the caller combines totals across excerpts.
- Respond with the JSON object and nothing else.
"""


def _upload_and_extract(file_path: str, mime_type: str, model_name: str):
    """Returns (data: dict, usage: dict) -- usage has input/output/thinking token counts.

    Lifecycle note: every call to client.files.upload() creates a remote
    file that lives in Gemini's File API storage independent of this
    process. Previously nothing ever deleted it -- for a production system
    processing large volumes of invoices/statements that leaks storage
    indefinitely (Gemini auto-expires files after 48h, but that still means
    up to two days of orphaned remote files at any given time, plus no
    guaranteed cleanup if a request path crashes before expiry). The upload
    is now wrapped so the remote file is deleted as soon as extraction
    finishes, success or failure, rather than relying solely on expiry."""
    client = _get_client()
    uploaded = client.files.upload(file=file_path, config={"mime_type": mime_type})
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[uploaded, EXTRACTION_PROMPT],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0,
            ),
        )

        text = (response.text or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        data = json.loads(text)  # may raise json.JSONDecodeError on truncated output -- caller handles it

        usage = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}
        um = getattr(response, "usage_metadata", None)
        if um is not None:
            usage["input_tokens"]    = getattr(um, "prompt_token_count", 0) or 0
            usage["output_tokens"]   = getattr(um, "candidates_token_count", 0) or 0
            usage["thinking_tokens"] = getattr(um, "thoughts_token_count", 0) or 0
        return data, usage
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception as exc:
            # Cleanup failure should never fail extraction -- log and move
            # on; the file will still auto-expire from Gemini's side.
            import logging
            logging.getLogger("saas.ai.gemini_extraction").warning(
                "[gemini] failed to delete uploaded file %s: %s",
                getattr(uploaded, "name", "?"), exc,
            )


def _validate(data: dict) -> None:
    if not isinstance(data, dict):
        raise ValueError("Gemini did not return a JSON object")
    if "Areas" not in data or "AgencyDetails" not in data:
        raise ValueError("Gemini response missing required top-level keys")


def _extract_one(file_path: str, mime_type: str, model_name: str, max_retries: int, _depth: int = 0):
    """
    Extract a single file (a whole document, or one already-split chunk).
    Retries max_retries times on ANY failure (network errors, invalid JSON).
    If JSON parsing specifically keeps failing (a strong signal the response
    was truncated because this chunk was too dense/large), and this is a
    multi-page PDF chunk, adaptively SPLITS IT IN HALF and retries each half
    -- down to a 1-page floor, at which point there's nothing left to split
    and the error is raised as-is.
    Returns (data, usage) same as _upload_and_extract, with usage summed
    across any adaptive sub-splits.
    """
    last_err = None
    saw_json_error = False
    for attempt in range(1, max_retries + 1):
        try:
            return _upload_and_extract(file_path, mime_type, model_name)
        except json.JSONDecodeError as e:
            last_err = e
            saw_json_error = True
        except Exception as e:
            last_err = e
        if attempt < max_retries:
            time.sleep(1.5 * attempt)

    if saw_json_error and mime_type == "application/pdf" and _depth < 4:
        page_count = _get_pdf_page_count(file_path)
        if page_count and page_count > 1:
            mid = page_count // 2
            left_path  = _split_pdf_pages(file_path, 0, mid)
            right_path = _split_pdf_pages(file_path, mid, page_count)
            try:
                left_data, left_usage   = _extract_one(left_path,  mime_type, model_name, max_retries, _depth + 1)
                right_data, right_usage = _extract_one(right_path, mime_type, model_name, max_retries, _depth + 1)
            finally:
                for p in (left_path, right_path):
                    try: os.remove(p)
                    except OSError: pass
            merged = _merge_chunk_results([left_data, right_data])
            merged_usage = {
                k: left_usage.get(k, 0) + right_usage.get(k, 0)
                for k in ("input_tokens", "output_tokens", "thinking_tokens")
            }
            return merged, merged_usage

    raise RuntimeError(f"Extraction failed after {max_retries} attempts: {last_err}")


# ═══════════════════════════════════════════════════════════════════════════
# PDF page counting / splitting
# ═══════════════════════════════════════════════════════════════════════════

def _get_pdf_page_count(path: str):
    try:
        from pypdf import PdfReader
        return len(PdfReader(path).pages)
    except Exception:
        return None  # not a PDF, corrupt, or pypdf unavailable -- caller treats as "don't chunk"


def _text_ratio_pypdf(path: str, page_idx: list, min_chars: int):
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        hits = 0
        for i in page_idx:
            try:
                t = reader.pages[i].extract_text() or ""
            except Exception:
                t = ""
            if len(t.strip()) >= min_chars:
                hits += 1
        return hits / len(page_idx)
    except Exception:
        return None


def _text_ratio_pdfminer(path: str, page_idx: list, min_chars: int):
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer
        hits = 0
        checked = 0
        # page_numbers lets pdfminer skip straight to the pages we want,
        # rather than sequentially processing the whole document.
        for page_layout in extract_pages(path, page_numbers=set(page_idx)):
            checked += 1
            text = "".join(
                el.get_text() for el in page_layout if isinstance(el, LTTextContainer)
            )
            if len(text.strip()) >= min_chars:
                hits += 1
        if checked == 0:
            return None
        return hits / checked
    except Exception:
        return None


def _text_ratio_pymupdf(path: str, page_idx: list, min_chars: int):
    try:
        import fitz
        doc = fitz.open(path)
        hits = 0
        for i in page_idx:
            try:
                t = doc[i].get_text() or ""
            except Exception:
                t = ""
            if len(t.strip()) >= min_chars:
                hits += 1
        doc.close()
        return hits / len(page_idx)
    except Exception:
        return None


def _pdf_has_text_layer(path: str, sample_pages: int = 30, min_chars_per_page: int = 40,
                         coverage_threshold: float = 0.8) -> bool:
    """
    Distinguishes a PURE-TEXT PDF (a real, selectable text layer -- e.g.
    exported from Tally/Excel/an ERP, or a native "Print to PDF") from a
    SCANNED/IMAGE PDF (a photo or scan with no embedded text, or only a thin
    OCR text layer) or a mixed PDF with embedded images/logos/stamps.

    Cross-checks THREE independent PDF text-extraction libraries -- pypdf,
    pdfminer.six, and PyMuPDF -- because any single one of them can
    under-detect a real text layer on certain PDF encodings/font-embedding
    schemes, which would incorrectly route a cheap, easy document to the
    pricier visual model. If ANY of the available libraries finds real text
    on at least `coverage_threshold` of the sampled pages, this counts as a
    genuine text layer -- this check runs in the background OCR worker (not
    on the HTTP request thread), so the extra latency of checking three
    libraries costs nothing in perceived responsiveness.

    Samples up to `sample_pages` pages spread across the WHOLE document (not
    just the first few, since a cover page or letterhead can be misleadingly
    text-light or text-heavy compared to the actual data pages).
    """
    try:
        from pypdf import PdfReader
        n = len(PdfReader(path).pages)
    except Exception:
        return False
    if n == 0:
        return False

    if n <= sample_pages:
        page_idx = list(range(n))
    else:
        step = n / sample_pages
        page_idx = sorted(set(int(i * step) for i in range(sample_pages)))

    ratios = []
    for fn in (_text_ratio_pypdf, _text_ratio_pdfminer, _text_ratio_pymupdf):
        r = fn(path, page_idx, min_chars_per_page)
        if r is not None:
            ratios.append(r)

    if not ratios:
        return False  # no library could read this file at all -- safer to use the visual model
    return max(ratios) >= coverage_threshold


def _category_for(file_path: str, mime_type: str) -> str:
    """
    Maps a document to its routing category (see model_registry.CATEGORIES).
    The superadmin "AI Models" page can override which Gemini model each
    category uses without a restart; this detection decides which category a
    document falls into:
      - PDFs: a genuine text layer (_pdf_has_text_layer) -> pdf_text, else
        pdf_scan. The three-library check below is expensive, so it only
        runs for PDFs.
      - Images -> image. Excel/CSV -> xlsx. Word -> docx. Text -> text.
      - Anything unrecognised -> image (the visual category that historically
        handled "everything else").
    """
    ext = os.path.splitext(file_path)[1].lower()
    if mime_type == "application/pdf":
        return "pdf_text" if _pdf_has_text_layer(file_path) else "pdf_scan"
    if mime_type and mime_type.startswith("image/"):
        return "image"
    if (mime_type in ("application/vnd.ms-excel",
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                      "text/csv") or ext in (".xlsx", ".xls", ".csv")):
        return "xlsx"
    if (mime_type in ("application/msword",
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            or ext in (".docx", ".doc")):
        return "docx"
    if mime_type == "text/plain" or ext in (".txt", ".log"):
        return "text"
    return "image"


def _select_model(file_path: str, mime_type: str) -> str:
    """
    Per-document model routing. The category is detected first (_category_for,
    based on real accuracy testing -- pure-text PDFs are far cheaper to
    extract and the visual model is reserved for scans/images), then the model
    for that category is resolved from the superadmin model settings: a saved
    per-category override wins, otherwise the .env default applies (see
    saas/ai/model_registry.py resolve_model()). Returns a model ID string.
    """
    return model_registry.resolve_model(_category_for(file_path, mime_type))


def _split_pdf_pages(src_path: str, start: int, end: int) -> str:
    """
    Module-level (picklable) function: writes pages [start, end) of src_path
    to a new temp PDF file and returns its path. Used both directly (adaptive
    retry, single-threaded) and via ProcessPoolExecutor (bulk initial chunking
    below) -- must stay a plain top-level function for multiprocessing to be
    able to pickle/send it to worker processes.
    """
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(src_path)
    writer = PdfWriter()
    for i in range(start, min(end, len(reader.pages))):
        writer.add_page(reader.pages[i])
    fd, out_path = tempfile.mkstemp(suffix=".pdf")
    with os.fdopen(fd, "wb") as f:
        writer.write(f)
    return out_path


def _split_pdf_into_chunks(path: str, max_pages: int, page_count: int):
    """
    Splits a large PDF into ~max_pages-page chunk files, IN PARALLEL, using a
    ProcessPoolExecutor. This step (pypdf re-encoding pages into new PDF
    files) is genuinely CPU-bound, unlike the Gemini calls below -- real
    multiprocessing (separate OS processes, no shared GIL) is the correct
    tool here, and pays off proportionally to page count / CPU core count.
    Returns a list of temp file paths, in original page order.
    """
    boundaries = [(s, min(s + max_pages, page_count)) for s in range(0, page_count, max_pages)]
    workers = min(len(boundaries), os.cpu_count() or 4)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_split_pdf_pages, path, s, e) for s, e in boundaries]
        return [f.result() for f in futures]  # preserves submission (page) order


def _merge_chunk_results(chunk_dicts):
    """
    Combines multiple chunk extraction results (each covering a different
    page range of the same document) into one. Areas/Stores/Items are
    concatenated (every chunk contributes its own parties/line-items).
    AgencyDetails/ReportDetails/DocType are taken from the first chunk that
    has non-empty values (statement header info is typically on page 1 and
    repeated or absent on later pages). InvoiceTotal/NetTotal use whichever
    chunk reports the largest non-zero value, preferring later chunks
    (grand totals are conventionally printed on the final page).
    """
    merged = {
        "AgencyDetails": {"Name": "", "GSTIN": "", "Address": ""},
        "ReportDetails": {"Company": "", "InvoiceNumber": "", "InvoiceDate": "", "FromDate": "", "ToDate": ""},
        "DocType": "STATEMENT",
        "Areas": [],
        "InvoiceTotal": 0,
        "NetTotal": 0,
        "ConfidenceScore": 0.0,
        "ValidationNotes": "",
    }
    notes = []
    for chunk in chunk_dicts:
        if not isinstance(chunk, dict):
            continue
        ag = chunk.get("AgencyDetails", {}) or {}
        for k in ("Name", "GSTIN", "Address"):
            if not merged["AgencyDetails"].get(k) and ag.get(k):
                merged["AgencyDetails"][k] = ag[k]
        rd = chunk.get("ReportDetails", {}) or {}
        for k in ("Company", "InvoiceNumber", "InvoiceDate", "FromDate", "ToDate"):
            if not merged["ReportDetails"].get(k) and rd.get(k):
                merged["ReportDetails"][k] = rd[k]
        if chunk.get("DocType"):
            merged["DocType"] = chunk["DocType"]
        merged["Areas"].extend(chunk.get("Areas", []) or [])
        for total_key in ("InvoiceTotal", "NetTotal"):
            v = float(chunk.get(total_key, 0) or 0)
            if v > 0:
                merged[total_key] = v  # later (later-page) chunks win on ties/overwrite
        if chunk.get("ValidationNotes"):
            notes.append(str(chunk["ValidationNotes"]))
    if notes:
        merged["ValidationNotes"] = " | ".join(notes)[:2000]
    return merged


def call_gemini_extraction(file_path: str, file_type: str, original_name: str,
                            max_retries: int = 3, company_id=None, upload_id=None,
                            division_id=None, user_id=None):
    """
    Extract a document using Gemini. Model is chosen ONCE per document (see
    _select_model()) -- gemini-2.5-flash for pure-text PDFs, gemini-3.5-flash
    for everything else (images, and scanned/image PDFs) -- and every chunk
    of that document (if it's large enough to be split) uses that same
    model, so cost/behavior stays consistent within one document rather than
    varying chunk-to-chunk. Retries up to `max_retries` times on
    failure/invalid JSON; raises on final failure so the caller
    (saas/extraction.py's upload flow) routes the document to Manual
    Verification, exactly like the original OCR pipeline's error handling.

    Documents over GEMINI_MAX_PAGES_PER_CHUNK pages are split and extracted
    in parallel (see module docstring). company_id/upload_id, if given, are
    used to log token usage + computed cost (against whichever model was
    actually used) to ai_usage_log (platform DB) for the superadmin AI
    Costing dashboard -- all optional so this function still works standalone
    (e.g. from a script) without a DB available. division_id, when provided,
    tenant-annotates that usage row.
    """
    mime = file_type or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    model_name = _select_model(file_path, mime)

    page_count = _get_pdf_page_count(file_path) if mime == "application/pdf" else None
    chunk_paths = None

    try:
        if page_count and page_count > GEMINI_MAX_PAGES_PER_CHUNK:
            chunk_paths = _split_pdf_into_chunks(file_path, GEMINI_MAX_PAGES_PER_CHUNK, page_count)
            # Extract every chunk IN PARALLEL -- these are network calls (I/O-bound,
            # GIL released while waiting), so a ThreadPoolExecutor is the correct
            # tool here, not another process pool. GEMINI_CHUNK_PARALLELISM bounds
            # how many chunks of THIS ONE document run at once, separate from
            # OCR_WORKER_POOL_SIZE (which bounds extraction across different
            # documents) so one huge upload can't monopolize the whole pool.
            with ThreadPoolExecutor(max_workers=min(GEMINI_CHUNK_PARALLELISM, len(chunk_paths))) as pool:
                results = list(pool.map(lambda p: _extract_one(p, mime, model_name, max_retries), chunk_paths))
            chunk_data  = [r[0] for r in results]
            for d in chunk_data:
                _validate(d)
            data = _merge_chunk_results(chunk_data)
            total_usage = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}
            for _, usage in results:
                for k in total_usage:
                    total_usage[k] += usage.get(k, 0)
            n_chunks = len(chunk_paths)
        else:
            data, total_usage = _extract_one(file_path, mime, model_name, max_retries)
            _validate(data)
            n_chunks = 1

        data["raw_json"] = json.dumps(data)
        data["_model_used"] = model_name
        _log_ai_usage(company_id, upload_id, model_name, total_usage, n_chunks,
                      division_id, user_id, original_name)
        return data
    finally:
        if chunk_paths:
            for p in chunk_paths:
                try: os.remove(p)
                except OSError: pass


def _log_ai_usage(company_id, upload_id, model_name: str, usage: dict, chunk_count: int,
                  division_id=None, user_id=None, filename=None):
    """Best-effort cost logging -- never let a logging failure break extraction.
    Writes to ai_usage_log in the platform control-plane DB (see
    saas/platform_db.py); division_id, when given, tenant-annotates the row,
    and user_id / filename (tenant user + source document) feed the costing
    dashboard's per-user / per-document views."""
    try:
        from ..platform_db import get_db
        from .pricing import compute_cost
        cost_usd, cost_inr = compute_cost(
            model_name, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
            usage.get("thinking_tokens", 0),
        )
        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO ai_usage_log
            (upload_id, company_id, division_id, user_id, original_filename, model_name,
             input_tokens, output_tokens, thinking_tokens, chunk_count, cost_usd, cost_inr)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (upload_id, company_id, division_id, user_id, filename, model_name,
             usage.get("input_tokens", 0), usage.get("output_tokens", 0),
             usage.get("thinking_tokens", 0), chunk_count, cost_usd, cost_inr))
        conn.commit()
        conn.close()
    except Exception:
        pass
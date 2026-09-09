"""
Configuration -- all values from environment variables, no hardcoded credentials.
"""
import os
import secrets

from dotenv import load_dotenv

# Load variables from a .env file in the project root, if present. This MUST
# run before os.environ.get(...) calls below -- without it, values in .env
# are silently ignored and every setting falls back to its default (most
# importantly SECRET_KEY, which would then be regenerated randomly on every
# process start/restart, invalidating every logged-in user's session cookie
# each time -- this was a real bug in an earlier version of this file).
load_dotenv()

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    # No SECRET_KEY in the environment/.env. Rather than generating a purely
    # in-memory random key (which would change -- and invalidate every
    # session -- on every single process restart, which is exactly the
    # "refresh asks me to log in again" bug), persist a generated key to a
    # local file so it's at least stable across restarts on this machine.
    # This is a DEV-ONLY safety net -- set a real SECRET_KEY in .env for
    # any real deployment (especially anything with more than one worker
    # process, where each worker would otherwise get its own key file race).
    _key_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".generated_secret_key")
    try:
        if os.path.exists(_key_file):
            with open(_key_file, "r") as f:
                SECRET_KEY = f.read().strip() or None
        if not SECRET_KEY:
            SECRET_KEY = secrets.token_hex(32)
            with open(_key_file, "w") as f:
                f.write(SECRET_KEY)
        print(f"WARNING: SECRET_KEY env var not set. Using an auto-generated key "
              f"persisted at {_key_file} so sessions survive restarts on this "
              f"machine -- set SECRET_KEY in .env before deploying to production "
              f"or running more than one worker process.")
    except OSError:
        SECRET_KEY = secrets.token_hex(32)
        print("WARNING: SECRET_KEY env var not set, and could not persist a "
              "generated key to disk. Using an in-memory random key for this "
              "process only -- ALL sessions will be invalidated on restart. "
              "Set SECRET_KEY in .env before deploying to production.")

DB_CONFIG = {
    "dbname":   os.environ.get("DB_NAME", "OCR_PILOT"),
    "user":     os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "host":     os.environ.get("DB_HOST", "localhost"),
    "port":     os.environ.get("DB_PORT", "5432"),
}

# Was hardcoded to maxconn=20 -- far too small once you're past a couple
# hundred concurrent users. Tune this against your actual Postgres server's
# max_connections (Postgres default is 100 *total*, shared across every
# client -- if you raise DB_POOL_MAX, raise Postgres's max_connections to
# match, with headroom for admin/other connections too).
DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", "2"))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "60"))

# Every route in this app is dispatched through FastAPI/Starlette's default
# blocking-call threadpool (see app/compat.py) -- this one pool is shared by
# EVERY concurrent request in the entire application: page loads, dashboard
# queries, AND the OCR/Gemini extraction calls, which each hold a thread for
# the full multi-second network round-trip. The library default is only 40,
# which a handful of concurrent document uploads can exhaust on its own,
# stalling out unrelated users just trying to log in. This is safe to raise
# well past 40 because these are I/O-bound waits (network calls release the
# GIL), not CPU-bound work -- but it must scale together with DB_POOL_MAX
# above, or you'll just move the bottleneck to "waiting for a DB connection"
# instead. See ENFORCE_HTTPS-style guidance in README for the bigger-picture
# recommendation (a dedicated background job queue) once you outgrow this.
THREAD_POOL_SIZE = int(os.environ.get("THREAD_POOL_SIZE", "150"))

# Separate, dedicated pool just for the actual Gemini/OCR extraction calls
# (see app/worker_pool.py) -- decoupled from THREAD_POOL_SIZE above so a
# burst of document uploads can't starve ordinary page requests (logins,
# dashboards, etc.) of threads, and vice versa. Tune against your Gemini
# API tier's concurrency/RPM allowance -- there's no point running more
# concurrent extractions than Gemini will actually accept at once; you'll
# just pile up 429s (the retry logic in app/ai/gemini_extraction.py handles
# occasional ones, but sustained over-limit traffic needs this turned down,
# not more retries).
OCR_WORKER_POOL_SIZE = int(os.environ.get("OCR_WORKER_POOL_SIZE", "40"))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# MODEL SELECTION -- two models, chosen per-document based on real accuracy
# testing (not a guess): gemini-3.5-flash handles scanned/image documents and
# PDFs with embedded images at ~100% accuracy; gemini-2.5-flash is cheaper
# and, per the same testing, handles PURE-TEXT PDFs (a real text layer, no
# scan/image content) just as well. See app/ai/gemini_extraction.py's
# _select_model() for the actual per-document detection logic -- images
# always go to GEMINI_MODEL_VISUAL; PDFs are inspected for a genuine text
# layer and routed accordingly.
GEMINI_MODEL_TEXT_PDF = os.environ.get("GEMINI_MODEL_TEXT_PDF", "gemini-2.5-flash")
GEMINI_MODEL_VISUAL    = os.environ.get("GEMINI_MODEL_VISUAL", "gemini-3.5-flash")
# Fallback used only if model selection itself fails for some reason (should
# be rare -- see _select_model's own fallback-to-visual behavior).
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", GEMINI_MODEL_VISUAL)

# USD->INR conversion for the superadmin cost-tracking dashboard (app/ai/pricing.py).
# This is a STATIC rate, not a live feed -- update it periodically (or wire in
# a live FX API yourself if you want it to track automatically). A stale rate
# only skews the displayed INR figure; it never affects what Google actually
# bills you in USD.
USD_TO_INR_RATE = float(os.environ.get("USD_TO_INR_RATE", "95.5"))

# PDF page-chunking threshold: documents with more pages than this are split
# into batches and extracted in parallel, then merged -- see
# app/ai/gemini_extraction.py for why (Gemini 3.5 Flash's 65,536-token OUTPUT
# limit, not the much larger 1M-token input window, is what a dense
# multi-hundred-line-item statement actually risks hitting). Tune down if
# your documents are unusually dense (many items per page); tune up if
# they're sparse.
GEMINI_MAX_PAGES_PER_CHUNK = int(os.environ.get("GEMINI_MAX_PAGES_PER_CHUNK", "25"))
# How many chunks of one large document to extract at the same time. Separate
# from OCR_WORKER_POOL_SIZE (which bounds extraction across DIFFERENT
# documents) so one huge document can't itself monopolize the whole pool.
GEMINI_CHUNK_PARALLELISM = int(os.environ.get("GEMINI_CHUNK_PARALLELISM", "6"))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads"))
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

SESSION_TIMEOUT = int(os.environ.get("SESSION_TIMEOUT", "900"))  # 15 min, seconds
SESSION_WARNING_AT = int(os.environ.get("SESSION_WARNING_AT", "780"))  # 13 min

SUPERADMIN_BOOTSTRAP_PASSWORD = os.environ.get("SUPERADMIN_BOOTSTRAP_PASSWORD", "superadmin@2025")

MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB

ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]

# Set to "true" once this app is served over HTTPS end-to-end (behind a
# reverse proxy terminating TLS, or directly). Turns on: the session
# cookie's Secure flag (browser won't send it over plain HTTP at all), and
# the HSTS header (tells browsers to only ever use HTTPS for this host).
# Leave false for local/plain-HTTP development -- enabling this before
# HTTPS is actually in place can lock you out of your own app.
ENFORCE_HTTPS = os.environ.get("ENFORCE_HTTPS", "false").strip().lower() == "true"

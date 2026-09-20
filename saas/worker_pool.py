"""
Dedicated worker pool for document extraction. Merged home of the legacy
app/worker_pool.py; logic is identical.

WHY THIS EXISTS: every route is dispatched through FastAPI/Starlette's shared
blocking-call threadpool. Before this module existed, the /upload route ran the
ENTIRE Gemini extraction call -- several seconds of pure network waiting --
inside that same shared pool, holding one of its threads for the whole
round-trip. At any real volume, a burst of document uploads could exhaust that
pool and stall out unrelated users just trying to log in or view a dashboard,
since there'd be no free thread left to handle their request at all.

This gives OCR extraction its OWN bounded pool (OCR_WORKER_POOL_SIZE, default
40), completely separate from the request-handling threadpool. A flood of
uploads can now only ever queue up waiting for an OCR worker -- it can't take
down the rest of the app.

SCALING NOTE: this is still a single-process, in-memory thread pool. It's a
real, meaningful fix for one server -- but if you outgrow a single (bigger)
machine, the next step up is a real distributed task queue (Celery or RQ,
backed by Redis) so extraction workers can run on separate machines from the
web server and scale independently. The call pattern below
(`OCR_EXECUTOR.submit(...)`) is deliberately a thin wrapper for exactly that
reason: swapping this out for `celery_task.delay(...)` later is a small,
localized change, not a rewrite.
"""
from concurrent.futures import ThreadPoolExecutor

from .config import OCR_WORKER_POOL_SIZE

OCR_EXECUTOR = ThreadPoolExecutor(max_workers=OCR_WORKER_POOL_SIZE, thread_name_prefix="ocr-worker")
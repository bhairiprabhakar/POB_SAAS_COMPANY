"""
Gemini File API orphan cleanup (was app/ai/gemini_extraction.py's helper).

Moved into `saas/` as part of the app-unification: the platform scheduler runs
this once a day (project-wide, not per-tenant -- Gemini File API storage isn't
tenant-scoped). Self-contained: reads the API key from saas/config and talks
to the Google GenAI SDK directly; it needs no database, so the legacy AI
pipeline modules are NOT required for it to work.
"""
import datetime
import logging

from google import genai

from . import config

log = logging.getLogger("saas.ai_cleanup")

_client = None


def _get_client():
    global _client
    if _client is None:
        if not config.GOOGLE_API_KEY:
            raise RuntimeError("GOOGLE_API_KEY/GEMINI_API_KEY is not set")
        _client = genai.Client(api_key=config.GOOGLE_API_KEY)
    return _client


def cleanup_orphaned_gemini_files(older_than_seconds: int = 3600) -> int:
    """Delete any remote Gemini File API entries older than
    `older_than_seconds` that are still sitting in project storage.

    The extraction path already deletes its own upload as soon as extraction
    finishes, so in the normal case this finds nothing. This is the safety
    net for a process crash / OOM kill / unhandled exception that would
    otherwise leave the file orphaned until Gemini's own ~48h auto-expiry.
    Run once a day via saas/scheduler.py's daily pass. Returns the number
    deleted."""
    client = _get_client()
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(seconds=older_than_seconds))
    deleted = 0
    try:
        for f in client.files.list():
            create_time = getattr(f, "create_time", None)
            if create_time is None or create_time >= cutoff:
                continue
            try:
                client.files.delete(name=f.name)
                deleted += 1
            except Exception as exc:
                log.warning("[gemini] failed to delete orphaned file %s: %s", f.name, exc)
    except Exception:
        log.exception("[gemini] failed to list remote files for orphan cleanup")
    return deleted
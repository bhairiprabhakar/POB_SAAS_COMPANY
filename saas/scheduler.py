"""
Background scheduler (Phase 7).

Runs in a daemon thread started at app startup. Work is queued in the tenant
`job_queue` table and drained with backoff:

  - webhook.deliver     first delivery attempt for a webhook event (Phase 6)
  - webhook.retry       re-deliver a failed webhook (from Phase 6)
  - payout.schedule     create due weekly/monthly payout batches
  - notification.followup  remind approvers about pending verifications
  - maintenance         purge expired refresh tokens

`run_scheduler_once()` performs a single sweep across every tenant database so
integration tests can drive the scheduler deterministically (no sleeps).
"""
import datetime
import json
import logging
import threading
import time

from . import config
from . import pools

log = logging.getLogger("saas.scheduler")

POLL_INTERVAL = int(getattr(config, "SCHEDULER_INTERVAL", "60") or 60)


def tenant_db_names() -> list[str]:
    from .db_utils import admin_conn
    conn = admin_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s AND datallowconn=TRUE",
            (config.TENANT_DB_PREFIX + "%",),
        )
        return [row[0] for row in c.fetchall()]
    finally:
        conn.close()


# ── Job execution ───────────────────────────────────────────────────────────

def _mark(conn, job_id: int, status: str, error: str = "", attempts: int = None,
          run_at=None):
    c = conn.cursor()
    if status == "running":
        c.execute("UPDATE job_queue SET status='running', started_at=CURRENT_TIMESTAMP WHERE id=%s", (job_id,))
    elif status == "queued":
        if run_at is None:
            run_at = datetime.datetime.now() + datetime.timedelta(minutes=1)
        c.execute(
            """UPDATE job_queue SET status='queued', error=%s, attempts=COALESCE(%s, attempts),
               run_at=%s WHERE id=%s""",
            (error[:2000] if error else None, attempts, run_at, job_id),
        )
    else:
        c.execute(
            """UPDATE job_queue SET status=%s, error=%s, finished_at=CURRENT_TIMESTAMP,
               attempts=COALESCE(%s, attempts) WHERE id=%s""",
            (status, error[:2000] if error else None, attempts, job_id),
        )
    conn.commit()


def _requeue(conn, job_id: int, attempts: int, error: str = "") -> None:
    """Re-queue with exponential backoff: run_at += attempts * 2 minutes."""
    run_at = datetime.datetime.now() + datetime.timedelta(minutes=max(1, attempts) * 2)
    _mark(conn, job_id, "queued", error, attempts, run_at)


def _process_job(conn, job: dict) -> None:
    job_type = job["job_type"]
    payload = job.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    attempts = (job.get("attempts") or 0) + 1
    max_attempts = job.get("max_attempts") or 5

    try:
        if job_type in ("webhook.deliver", "webhook.retry"):
            from . import webhooks
            res = webhooks.deliver_webhook(conn, payload["webhook_id"], payload["delivery_id"])
            ok = res.get("status") == "delivered"
        elif job_type == "payout.schedule":
            ok = _create_due_payout_batches(conn) >= 0
        elif job_type == "notification.followup":
            ok = _send_followup_reminders(conn)
        elif job_type == "maintenance":
            ok = _maintenance(conn)
        else:
            _mark(conn, job["id"], "failed", f"unknown job_type {job_type}", attempts)
            return

        if ok:
            _mark(conn, job["id"], "done", attempts=attempts)
        elif attempts >= max_attempts:
            _mark(conn, job["id"], "failed", "max attempts reached", attempts)
        else:
            _requeue(conn, job["id"], attempts, "retry")
    except Exception as exc:  # job-level safety
        log.exception("job %s (%s) crashed", job["id"], job_type)
        if attempts >= max_attempts:
            _mark(conn, job["id"], "failed", str(exc), attempts)
        else:
            _requeue(conn, job["id"], attempts, str(exc))


def _due_jobs(conn) -> list[dict]:
    from .db_utils import fetchall_dict
    c = conn.cursor()
    c.execute(
        """SELECT * FROM job_queue
           WHERE status IN ('queued') AND run_at <= CURRENT_TIMESTAMP
           ORDER BY run_at, id LIMIT 50 FOR UPDATE SKIP LOCKED""",
    )
    return fetchall_dict(c)


def process_tenant_jobs(tenant_db: str) -> int:
    """Drain due jobs for one tenant. Returns the number processed."""
    conn = pools.get_tenant_conn(tenant_db)
    try:
        from . import migrations
        migrations.ensure_migrated(tenant_db)
        jobs = _due_jobs(conn)
        for job in jobs:
            _process_job(conn, job)
        return len(jobs)
    finally:
        conn.close()


# ── Periodic housekeeping (run when no queued jobs exist) ───────────────────

_last_daily_date: str | None = None


def _daily_pass():
    """Once-per-day housekeeping: visit.due reminders + platform backups.
    Runs only from the daemon loop so test calls to run_scheduler_once()
    stay deterministic."""
    global _last_daily_date
    today = datetime.date.today().isoformat()
    if _last_daily_date == today:
        return
    _last_daily_date = today
    for tenant_db in tenant_db_names():
        try:
            conn = pools.get_tenant_conn(tenant_db)
            try:
                from . import migrations
                migrations.ensure_migrated(tenant_db)
                _send_visit_due_reminders(conn)
                _send_proof_reminders(conn)
                _activate_scheduled_campaigns(conn)
            finally:
                conn.close()
        except Exception:
            log.exception("daily sweep failed for %s", tenant_db)
    try:
        from .backup import run_backup_all
        summary = run_backup_all()
        log.info("daily backup: ok=%s fail=%s", summary["ok_count"], summary["fail_count"])
    except Exception:
        log.exception("daily backup sweep failed")
    try:
        from app.ai.gemini_extraction import cleanup_orphaned_gemini_files
        deleted = cleanup_orphaned_gemini_files()
        log.info("gemini orphaned-file sweep: deleted=%s", deleted)
    except Exception:
        log.exception("gemini orphaned-file sweep failed")

def _activate_scheduled_campaigns(conn) -> int:
    """Approved campaigns whose start window has opened move scheduled -> active."""
    c = conn.cursor()
    c.execute("""UPDATE campaigns SET status='active'
                 WHERE status='scheduled' AND start_date IS NOT NULL
                   AND start_date <= CURRENT_DATE""")
    n = c.rowcount
    if n:
        conn.commit()
    return int(n)


def _create_due_payout_batches(conn) -> int:
    """Create open weekly/monthly batches for campaigns whose payout date is
    today (or overdue). Returns the number of batches created."""
    c = conn.cursor()
    today = datetime.date.today()
    weekday = today.weekday()  # 0=Mon .. 6=Sun
    c.execute("""SELECT c.* FROM campaigns c
                 WHERE c.status IN ('active','scheduled') AND c.payout_cycle IN ('weekly','monthly')
                   AND c.payout_weekday IS NOT NULL AND c.payout_month_day IS NOT NULL""")
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    campaigns = [dict(zip(cols, r)) for r in rows]
    created = 0
    for cm in campaigns:
        due = False
        if cm["payout_cycle"] == "weekly":
            due = weekday == (cm["payout_weekday"] or 6)
        else:
            due = today.day == (cm["payout_month_day"] or 1)
        if not due:
            continue
        c.execute(
            """SELECT g.id FROM gratifications g
               WHERE g.type_code IN ('cashback','upi') AND g.status='approved'
                 AND COALESCE(g.payout_cycle,'instant')=%s
                 AND COALESCE(g.payout_batch_date,CURRENT_DATE) <= CURRENT_DATE
                 AND NOT EXISTS (SELECT 1 FROM payout_batch_items i WHERE i.gratification_id=g.id)""",
            (cm["payout_cycle"],),
        )
        ids = [r[0] for r in c.fetchall()]
        if not ids:
            continue
        c.execute("SELECT COALESCE(SUM(scheme_value),0) FROM gratifications WHERE id = ANY(%s)", (ids,))
        total = c.fetchone()[0]
        c.execute("""INSERT INTO payout_batches (cycle, period_start, period_end, total_amount,
                     status) VALUES (%s, CURRENT_DATE, CURRENT_DATE, %s, 'open') RETURNING id""",
                  (cm["payout_cycle"], total))
        bid = c.fetchone()[0]
        for gid in ids:
            c.execute("""INSERT INTO payout_batch_items (batch_id, gratification_id, amount)
                         SELECT %s, id, scheme_value FROM gratifications WHERE id=%s""", (bid, gid))
        created += 1
    conn.commit()
    return created


def _send_followup_reminders(conn) -> bool:
    """Notify verifiers with pending approval items once a day."""
    c = conn.cursor()
    c.execute("""SELECT COUNT(*) FROM pob_verifications WHERE status='pending'""")
    pending = c.fetchone()[0]
    if not pending:
        return True
    c.execute("""SELECT DISTINCT v.verifier_id FROM pob_verifications v WHERE v.status='pending'""")
    verifier_ids = [r[0] for r in c.fetchall()]
    from .notify import notify_user
    for vid in verifier_ids:
        notify_user(conn, vid, "followup.due",
                    "Pending verifications", f"You have {pending} POB(s) pending verification.",
                    "verification", None)
    return True


def _maintenance(conn) -> bool:
    c = conn.cursor()
    c.execute("DELETE FROM refresh_tokens WHERE expires_at < %s", (int(time.time()) - 86400,))
    c.execute("""UPDATE webhook_deliveries SET status='failed'
                 WHERE status IN ('pending') AND created_at < CURRENT_TIMESTAMP - INTERVAL '1 day'""")
    conn.commit()
    return True


def _send_visit_due_reminders(conn) -> bool:
    """Remind PSRs once a day about chemists due for the 15-day follow-up
    visit + stock liquidation check (no visit ever / last visit > 15 days)."""
    c = conn.cursor()
    c.execute(
        """SELECT pa.user_id, count(DISTINCT ch.id) AS due_count
           FROM chemists ch
           JOIN pob_activities pa ON pa.chemist_id=ch.id AND pa.status='verified'
           WHERE pa.created_at >= current_date - INTERVAL '120 days'
             AND COALESCE((SELECT max(cv2.visit_date) FROM chemist_visits cv2
                           WHERE cv2.chemist_id=ch.id), current_date)
                 <= current_date - INTERVAL '15 days'
           GROUP BY pa.user_id""",
    )
    rows = c.fetchall()
    from .notify import notify_from_template
    for uid, cnt in rows:
        notify_from_template(conn, uid, "visit.due", {"count": cnt}, "visit", None)
    return True


def _send_proof_reminders(conn) -> bool:
    """Remind field users (MR/PSR) once a day, after a campaign has ended
    (grace window included), about submitted POBs that still carry no invoice
    proof. Only the submitting user is notified."""
    c = conn.cursor()
    c.execute(
        """SELECT * FROM campaigns
           WHERE status IN ('active','completed') AND end_date IS NOT NULL
             AND (end_date + (COALESCE(grace_months,0) * INTERVAL '1 month')
                            + (COALESCE(grace_days,15) * INTERVAL '1 day')) < CURRENT_DATE""",
    )
    cols = [d[0] for d in c.description]
    campaigns = [dict(zip(cols, r)) for r in c.fetchall()]
    from .notify import notify_from_template
    for cm in campaigns:
        c.execute(
            """SELECT pa.user_id, count(*) AS cnt
               FROM pob_activities pa
               WHERE pa.campaign_id=%s AND pa.status='submitted'
               GROUP BY pa.user_id""",
            (cm["id"],),
        )
        for uid, cnt in c.fetchall():
            c.execute(
                """SELECT count(*) FROM notifications
                   WHERE user_id=%s AND type='pob.proof.due'
                     AND created_at::date = CURRENT_DATE""",
                (uid,),
            )
            if c.fetchone()[0]:
                continue
            notify_from_template(conn, uid, "pob.proof.due",
                                 {"campaign": cm["name"], "count": cnt}, "pob", None)
    conn.commit()
    return True


# ── Sweep driver ────────────────────────────────────────────────────────────

def run_scheduler_once() -> int:
    """One sweep across all tenants: drain due jobs + run housekeeping.
    Returns the total number of jobs processed."""
    total = 0
    for tenant_db in tenant_db_names():
        try:
            total += process_tenant_jobs(tenant_db)
        except Exception:
            log.exception("scheduler sweep failed for %s", tenant_db)
    return total


def _loop():
    while True:
        try:
            run_scheduler_once()
        except Exception:
            log.exception("scheduler sweep crashed")
        try:
            _daily_pass()
        except Exception:
            log.exception("daily pass crashed")
        time.sleep(POLL_INTERVAL)


_thread: threading.Thread | None = None
_started = False


def start_scheduler():
    """Idempotent daemon-thread startup (also drives webhook retries)."""
    global _thread, _started
    if _started:
        return
    _started = True
    _thread = threading.Thread(target=_loop, name="pob-scheduler", daemon=True)
    _thread.start()
    log.info("scheduler started (interval=%ss)", POLL_INTERVAL)

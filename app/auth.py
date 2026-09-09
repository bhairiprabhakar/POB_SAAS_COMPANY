import time
from functools import wraps

from .compat import request, session, redirect, url_for, flash
from .config import SESSION_TIMEOUT
from .helpers import get_user


def _touch_or_expire(id_key):
    """
    Shared inactivity-timeout check for every decorator below.

    Returns True and refreshes session['last_activity'] if the session is
    still within SESSION_TIMEOUT seconds of its last authenticated request.
    Returns False (and clears the session's identity keys) if it's been
    inactive longer than that -- the caller should then redirect to login.

    NOTE: this only extends the session on requests that pass through one of
    login_required/admin_required/superadmin_required/agent_required -- i.e.
    real authenticated actions, not just mouse movement in the browser. The
    frontend's periodic GET /api/session/status call (see main_routes.py)
    deliberately does NOT go through these decorators, so merely checking
    the remaining time does not itself reset the clock -- only genuine
    activity (a page load, an API call, clicking "Stay logged in") does.
    """
    now = time.time()
    last = session.get("last_activity")
    if last is not None and (now - last) > SESSION_TIMEOUT:
        session.pop(id_key, None)
        session.pop("last_activity", None)
        return False
    session["last_activity"] = now
    return True


def login_required(f):
    @wraps(f)
    def dec(*a, **kw):
        uid = session.get("user_id")
        if not uid:
            return redirect(url_for("login"))
        if not _touch_or_expire("user_id"):
            session.pop("company_id", None)
            flash("Your session expired after 15 minutes of inactivity. Please log in again.")
            return redirect(url_for("login"))
        user = get_user(uid)
        if not user or user.get("status") != "active":
            # Covers both "user no longer exists" and "user was just
            # deactivated" -- either way, an already-open session for this
            # user must not keep working past that point.
            session.pop("user_id", None)
            session.pop("company_id", None)
            if user:  # existed but is inactive/suspended/left, not deleted
                flash("Your account is no longer active. Contact your admin.")
            return redirect(url_for("login"))
        return f(*a, **kw)
    return dec


def admin_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not _touch_or_expire("user_id"):
            session.pop("company_id", None)
            flash("Your session expired after 15 minutes of inactivity. Please log in again.")
            return redirect(url_for("login"))
        u = get_user(session["user_id"])
        if not u or u["role"] not in ("company_admin", "nsm", "zsm"):
            flash("Admin access required")
            return redirect(url_for("dashboard"))
        return f(*a, **kw)
    return dec


def superadmin_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if "super_admin_id" not in session:
            return redirect(url_for("sa_login"))
        if not _touch_or_expire("super_admin_id"):
            session.pop("super_admin_role", None)
            flash("Your session expired after 15 minutes of inactivity. Please log in again.")
            return redirect(url_for("sa_login"))
        if session.get("super_admin_role") == "agent":
            return redirect(url_for("agent_dashboard"))
        return f(*a, **kw)
    return dec


def agent_required(f):
    """Decorator for agent-only routes."""
    @wraps(f)
    def dec(*a, **kw):
        if "super_admin_id" not in session:
            return redirect(url_for("sa_login"))
        if not _touch_or_expire("super_admin_id"):
            session.pop("super_admin_role", None)
            flash("Your session expired after 15 minutes of inactivity. Please log in again.")
            return redirect(url_for("sa_login"))
        if session.get("super_admin_role") not in ("agent", "superadmin"):
            flash("Access denied")
            return redirect(url_for("sa_login"))
        return f(*a, **kw)
    return dec

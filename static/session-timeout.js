/*
 * Session-timeout warning (15-min inactivity, 13-min warning).
 * Included by index.html, agent.html, superadmin.html.
 * Set window.SESSION_LOGIN_URL before this script tag to control where an
 * expired session redirects to (each portal has its own login page).
 */
(function () {
  var LOGIN_URL = window.SESSION_LOGIN_URL || "/login";
  var POLL_MS = 15000;
  var modalEl = null;

  function fmt(s) {
    s = Math.max(0, s | 0);
    var m = Math.floor(s / 60), ss = s % 60;
    return m + ":" + (ss < 10 ? "0" : "") + ss;
  }

  function showModal(remaining) {
    if (modalEl) { updateCountdown(remaining); return; }
    modalEl = document.createElement("div");
    modalEl.id = "session-timeout-modal";
    modalEl.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:99999;" +
      "display:flex;align-items:center;justify-content:center;font-family:'Inter',system-ui,sans-serif";
    modalEl.innerHTML =
      '<div style="background:var(--bg1,#fff);padding:26px 30px;border-radius:16px;max-width:340px;' +
      'text-align:center;box-shadow:0 24px 64px rgba(0,0,0,.3)">' +
        '<div style="font-size:15px;font-weight:700;margin-bottom:6px;color:var(--t0,#1D1D1F)">Session expiring soon</div>' +
        '<div style="font-size:12.5px;color:var(--t2,#8E8E93);line-height:1.4">You\u2019ve been inactive for a while ' +
        'and will be logged out automatically.</div>' +
        '<div id="session-timeout-countdown" style="font-size:30px;font-weight:700;font-family:var(--mono),monospace;' +
        'color:var(--red,#D70015);margin:14px 0">' + fmt(remaining) + '</div>' +
        '<button id="session-stay-btn" style="padding:10px 26px;background:var(--blue,#0071E3);border:none;' +
        'border-radius:9px;color:#fff;font-size:13px;font-weight:600;cursor:pointer;width:100%">Stay logged in</button>' +
      '</div>';
    document.body.appendChild(modalEl);
    document.getElementById("session-stay-btn").addEventListener("click", keepAlive);
  }

  function hideModal() {
    if (modalEl) { modalEl.remove(); modalEl = null; }
  }

  function updateCountdown(remaining) {
    var el = document.getElementById("session-timeout-countdown");
    if (el) el.textContent = fmt(remaining);
  }

  function keepAlive() {
    fetch("/api/session/keepalive", { method: "POST" })
      .then(function () { hideModal(); })
      .catch(function () {});
  }

  function checkStatus() {
    fetch("/api/session/status")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.authenticated) { hideModal(); return; }
        if (d.remaining <= 0) {
          window.location.href = LOGIN_URL;
          return;
        }
        if (d.remaining <= d.warn_at_remaining) {
          showModal(d.remaining);
        } else {
          hideModal();
        }
      })
      .catch(function () {});
  }

  checkStatus();
  setInterval(checkStatus, POLL_MS);
})();

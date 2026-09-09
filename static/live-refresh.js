/*
 * Generic "live badge" auto-refresh.
 * Add data-live-badge="/some/api/endpoint:jsonKey" to any element (a count
 * badge, a small status indicator, etc). This script polls each distinct
 * URL once every POLL_MS, reads jsonKey out of the JSON response, and:
 *   - sets the element's text to that value
 *   - shows the element if value > 0, hides it (display:none) if falsy
 * Multiple badges pointing at the same URL only trigger one fetch per tick.
 * Silently does nothing on pages with no [data-live-badge] elements, so
 * including this script everywhere is harmless.
 *
 * Also supports a page-level opt-in for auto-refreshing an entire list view:
 *   <body data-auto-refresh-page="45">
 * reloads the page every 45s -- ONLY use this on read-only list/queue pages,
 * never on a page with an in-progress editor (nothing in this app currently
 * sets this on an editable page; double-check before adding it to a new one).
 */
(function () {
  var POLL_MS = 20000;

  function refreshBadges() {
    var els = document.querySelectorAll("[data-live-badge]");
    if (!els.length) return;

    var byUrl = {};
    els.forEach(function (el) {
      var spec = el.getAttribute("data-live-badge");
      var idx = spec.lastIndexOf(":");
      var url = spec.slice(0, idx);
      var key = spec.slice(idx + 1);
      (byUrl[url] = byUrl[url] || []).push({ el: el, key: key });
    });

    Object.keys(byUrl).forEach(function (url) {
      fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (d) {
          byUrl[url].forEach(function (entry) {
            var val = d[entry.key];
            if (val === undefined || val === null) return;
            entry.el.textContent = val;
            entry.el.style.display = (Number(val) > 0) ? "" : "none";
          });
        })
        .catch(function () {});
    });
  }

  refreshBadges();
  setInterval(refreshBadges, POLL_MS);

  var refreshSeconds = parseInt(document.body.getAttribute("data-auto-refresh-page") || "0", 10);
  if (refreshSeconds > 0) {
    setInterval(function () {
      // Don't yank the page out from under someone mid-interaction with an
      // open modal/dialog (best-effort check -- covers this app's modals,
      // which all use a fixed-position overlay div), or while a file upload
      // is actively being processed (window._activeUploads, set by the
      // upload/poll JS on pages that have a drop-zone).
      var hasOpenOverlay = !!document.querySelector(
        '[id$="-modal"]:not([style*="display: none"]):not([style*="display:none"])'
      );
      var uploadInProgress = (window._activeUploads || 0) > 0;
      if (!hasOpenOverlay && !uploadInProgress) location.reload();
    }, refreshSeconds * 1000);
  }
})();

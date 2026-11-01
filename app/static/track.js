// Behaviour tracking for the dashboard. See app/behaviour.py for what is stored and why, and /privacy.
// Only runs when the server marked the page data-track="on" (logged in, not opted out, no Global Privacy Control).
// The browser never sends a user or session id: the server reads the anonymous id from the signed cookie.
(() => {
  "use strict";
  const MAX_BATCH = 50;
  const tracking = document.body.dataset.track === "on";
  const queue = [];

  function push(a, s, extra) {
    if (!tracking || !s) return;
    queue.push(Object.assign({ a, s }, extra || {}));
    if (queue.length >= 20) flush();
  }

  function flush() {
    while (queue.length) {
      const body = JSON.stringify({ events: queue.splice(0, MAX_BATCH) });
      fetch("/api/events", {
        method: "POST", body, keepalive: true, credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
      }).catch(() => {});
    }
  }

  const idOf = (el) => Number(el.closest("[data-signal]")?.dataset.signal || 0);

  // ---- impressions and scroll-past --------------------------------------------------------------
  const seen = new Set(), opened = new Set(), passed = new Set(), timers = new Map();
  if (tracking && "IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const id = idOf(e.target);
        if (e.isIntersecting) {
          if (!seen.has(id) && !timers.has(id)) {
            // counts as seen once at least half of it has been on screen for a second
            timers.set(id, setTimeout(() => {
              seen.add(id); timers.delete(id);
              push("impression", id, { p: Number(e.target.dataset.position || 0) });
            }, 1000));
          }
        } else {
          clearTimeout(timers.get(id)); timers.delete(id);
          const scrolledPast = e.boundingClientRect.bottom < (e.rootBounds ? e.rootBounds.top : 0) + 1;
          if (seen.has(id) && !opened.has(id) && !passed.has(id) && scrolledPast) {
            passed.add(id); push("scroll_past", id);
          }
        }
      }
    }, { threshold: 0.5 });
    document.querySelectorAll("article.signal[data-signal]").forEach((el) => io.observe(el));
  }

  // ---- opens and dwell time (only while the page is visible) -----------------------------------
  const openSince = new Map();
  function stopDwell(id) {
    const t = openSince.get(id);
    if (t === undefined) return;
    openSince.delete(id);
    const seconds = (performance.now() - t) / 1000;
    if (seconds >= 1) push("dwell", id, { v: Math.round(seconds * 10) / 10 });
  }
  document.addEventListener("toggle", (e) => {
    const d = e.target;
    if (!(d instanceof HTMLDetailsElement) || !d.classList.contains("why-box")) return;
    const id = idOf(d);
    if (d.open) {
      if (!opened.has(id)) { opened.add(id); push("open", id); }
      openSince.set(id, performance.now());
    } else {
      stopDwell(id);
    }
  }, true);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      [...openSince.keys()].forEach(stopDwell);
      flush();
    } else {
      document.querySelectorAll("details.why-box[open]").forEach((d) => openSince.set(idOf(d), performance.now()));
    }
  });

  // ---- clicks through to the primary source ---------------------------------------------------
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a.source-link");
    if (a) { push("source_click", idOf(a)); flush(); }
  });

  // ---- Useful / Not useful without a page reload (the forms still work without JavaScript) -----
  document.addEventListener("submit", async (e) => {
    const form = e.target.closest("form.rate");
    if (!form) return;
    e.preventDefault();
    try {
      const r = await fetch(form.action, { method: "POST", credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(String(r.status));
      const { state } = await r.json();
      const box = form.closest(".rating");
      box.querySelectorAll("button[data-value]").forEach((b) => {
        const on = state !== null && Number(b.dataset.value) === state;
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      const live = box.querySelector("[role=status]");
      if (live) live.textContent = state === null ? "Rating cleared" : "Thanks, noted";
    } catch (_) {
      form.submit();
    }
  });

  setInterval(flush, 10000);
  window.addEventListener("pagehide", () => { [...openSince.keys()].forEach(stopDwell); flush(); });
})();

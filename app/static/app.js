// Small progressive enhancements. Everything works without JavaScript; this only adds confirmations.
// Kept in a file (not inline handlers) so the Content-Security-Policy can forbid inline script.
document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-confirm]");
  if (form && !window.confirm(form.dataset.confirm)) event.preventDefault();
});

// Live ticker search: same results as the server-rendered list, fetched as you type.
(() => {
  const input = document.getElementById("q");
  const list = document.getElementById("ticker-results");
  if (!input || !list) return;
  let timer = null, last = "";
  const el = (tag, props = {}, ...kids) => {
    const n = Object.assign(document.createElement(tag), props);
    kids.forEach((k) => n.append(k));
    return n;
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.trim();
      if (q === last) return;
      last = q;
      if (q.length < 1) { list.replaceChildren(); return; }
      try {
        const r = await fetch("/api/tickers?q=" + encodeURIComponent(q), { credentials: "same-origin" });
        if (!r.ok) return;
        const { results } = await r.json();
        list.replaceChildren(...results.map((t) => {
          const form = el("form", { method: "post", action: "/watchlist/add" },
            el("input", { type: "hidden", name: "symbol", value: t.symbol }),
            el("button", { textContent: "Add", ariaLabel: "Add " + t.symbol }));
          const cov = el("span", { className: "cov " + (t.coverage === "not covered yet" ? "cov-no" : "cov-yes"), textContent: t.coverage });
          const info = el("span", {}, el("b", { textContent: t.symbol }), " " + t.name, el("br"),
            el("span", { className: "small" }, t.exchange + " · ", cov));
          return el("li", {}, form, info);
        }));
      } catch (_) { /* keep the server-rendered list */ }
    }, 250);
  });
})();

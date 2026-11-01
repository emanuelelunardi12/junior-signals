// Small progressive enhancements. Everything works without JavaScript; this only adds confirmations.
// Kept in a file (not inline handlers) so the Content-Security-Policy can forbid inline script.
document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-confirm]");
  if (form && !window.confirm(form.dataset.confirm)) event.preventDefault();
});

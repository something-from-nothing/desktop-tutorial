// Passes the reader's messages from the boot.dev page to the extension's background worker.
window.addEventListener('message', function (e) {
  if (e.source !== window || !e.data || e.data.__bdDash !== 1 || typeof e.data.s !== 'string') return;
  try { chrome.runtime.sendMessage({ s: e.data.s }); } catch (err) { /* extension reloaded: ignore */ }
});

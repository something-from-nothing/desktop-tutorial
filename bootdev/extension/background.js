// Sends each update to the Boot.dev Dashboard app, which listens only on this PC.
chrome.runtime.onMessage.addListener(function (m) {
  if (!m || typeof m.s !== 'string') return;
  fetch('http://127.0.0.1:47822/ext/push', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: m.s
  }).catch(function () { /* app not open */ });
});

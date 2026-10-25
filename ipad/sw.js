// Keeps the app's own files on the iPad so it opens with no internet. (The AI model is
// stored separately by WebLLM the first time it downloads.)
const CACHE = "desk-agent-v2";
const FILES = ["./", "./index.html", "./ipad.js", "./webllm.js", "./wllama.js", "./wllama.wasm", "./manifest.webmanifest", "./icon-180.png", "./icon-192.png", "./icon-512.png"];
self.addEventListener("install", e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting())));
self.addEventListener("activate", e => e.waitUntil(clients.claim()));
// The network first, so updates arrive; the saved copy when offline.
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(fetch(e.request).then(res => {
    const copy = res.clone();
    caches.open(CACHE).then(c => c.put(e.request, copy));
    return res;
  }).catch(() => caches.match(e.request, {ignoreSearch: true})));
});

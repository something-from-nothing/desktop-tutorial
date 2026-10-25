#!/bin/bash
# Builds the iPad web app into ./site: the phone app's page, plus the bridge that runs
# the AI in Safari (ipad.js), WebLLM itself (from npm, version pinned) and the bits
# that let Safari add it to the Home Screen.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf site && mkdir site
WEBLLM=0.2.85
curl -fsSL -o webllm.tgz "https://registry.npmjs.org/@mlc-ai/web-llm/-/web-llm-$WEBLLM.tgz"
tar xzf webllm.tgz package/lib/index.js && mv package/lib/index.js site/webllm.js && rm -rf package webllm.tgz
# Older iPads (A12X/A12Z) allow 16 KB of GPU working memory per workgroup; WebLLM asks
# for 32 KB and refuses to start. Ask for no more than the device allows instead.
python3 - <<'PY'
p = "site/webllm.js"
s = open(p).read()
old = "const requiredMaxComputeWorkgroupStorageSize = 32 << 10;"
assert s.count(old) == 1, "WebLLM changed: check the workgroup storage request"
open(p, "w").write(s.replace(old, "const requiredMaxComputeWorkgroupStorageSize = Math.min(32 << 10, adapter.limits.maxComputeWorkgroupStorageSize);"))
PY
# wllama runs the model on the processor when the graphics chip can't.
WLLAMA=3.6.1
curl -fsSL -o wllama.tgz "https://registry.npmjs.org/@wllama/wllama/-/wllama-$WLLAMA.tgz"
tar xzf wllama.tgz package/esm/index.js package/esm/wasm/wllama.wasm
mv package/esm/index.js site/wllama.js && mv package/esm/wasm/wllama.wasm site/wllama.wasm && rm -rf package wllama.tgz
cp ipad.js sw.js manifest.webmanifest icon-*.png site/
python3 - <<'PY'
src = open("../android/app/src/main/assets/index.html").read()
head = """<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Desk Agent">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<script src="ipad.js"></script>
<script>if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js");
// Opened from a plain link: add the settings the Home Screen icon opens with.
if (!location.search.includes("builtin=")) history.replaceState(null, "", "index.html?engine=1&builtin=quick");</script>
"""
assert src.count("</title>") == 1
open("site/index.html", "w").write(src.replace("</title>", "</title>\n" + head, 1))
PY
# Every build gets its own file addresses (?v=...), so Safari and githack can't keep
# serving an older copy of the app after an update.
V=$(git rev-parse --short HEAD 2>/dev/null || date +%s)
sed -i "s#src=\"ipad.js\"#src=\"ipad.js?v=$V\"#; s#register(\"sw.js\")#register(\"sw.js?v=$V\")#" site/index.html
sed -i "s#\"./webllm.js\"#\"./webllm.js?v=$V\"#; s#\"./wllama.js\"#\"./wllama.js?v=$V\"#; s#\"wllama.wasm\"#\"wllama.wasm?v=$V\"#" site/ipad.js
sed -i "s#const CACHE = \"desk-agent-v[0-9]*\";#const CACHE = \"desk-agent-$V\";#" site/sw.js
grep -q "ipad.js?v=$V" site/index.html && grep -q "webllm.js?v=$V" site/ipad.js && grep -q "wllama.wasm?v=$V" site/ipad.js && grep -q "desk-agent-$V" site/sw.js
ls -la site

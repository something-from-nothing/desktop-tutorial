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
ls -la site

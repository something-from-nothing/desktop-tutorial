#!/usr/bin/env bash
# Zip each skill in .claude/skills into dist/ so it can be uploaded to claude.ai.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -f dist/*.zip
for dir in .claude/skills/*/; do
  name=$(basename "$dir")
  (cd .claude/skills && zip -qr "../../dist/${name}.zip" "$name")
  echo "built dist/${name}.zip"
done

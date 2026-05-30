#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "=== Removing duplicate files ==="
rm -f "docs/conversion-process 2.md" "docs/feature-gap-plan-v1.1 2.md" \
      "docs/pipeline-diagram 2.svg" "docs/security-review 2.md"

echo "=== Clearing git lock files ==="
rm -f .git/index.lock .git/HEAD.lock .git/refs/heads/main.lock .git/refs/heads/main.lock.bak

echo "=== Staging and committing ==="
git add -A
git diff --cached --stat
git commit -m "chore: remove macOS duplicate files" || echo "(nothing to commit)"

echo "=== Pushing to origin/main ==="
git push origin main

echo "=== Tagging v1.4.0 ==="
git tag -f v1.4.0
git push origin v1.4.0 --force

echo ""
echo "✅ Done! specmcp v1.4.0 is live on GitHub."

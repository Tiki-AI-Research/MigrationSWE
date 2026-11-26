#!/bin/bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${SWEBENCH_REPO_PATH:-/workspace/repo}"
PATCH="${SOLUTION_PATCH_PATH:-${SCRIPT_DIR}/solution.patch}"
BASE_COMMIT=20269154bb9aab9672e7ff7ca500349e5d23fb5c
BRANCH=migration/koa2-middleware

cd "$REPO"

if [ "$(git rev-parse HEAD)" != "$BASE_COMMIT" ]; then
  echo "[solve] expected HEAD $BASE_COMMIT, got $(git rev-parse HEAD)" >&2
  exit 1
fi

git config core.fileMode false
git config user.email "oracle@example.com"
git config user.name "Migration Oracle"
git switch -c "$BRANCH"

git update-index --refresh >/dev/null 2>&1 || true
git apply --check --binary "$PATCH"
git apply --index --binary "$PATCH"

# SOL-029: prove the oracle patch is exactly reversible on the base tree. A
# patch that cannot be reverted cannot be used to reconstruct the base, and the
# hard gate refuses to trust anything but a content-derived base tree.
if ! git apply --check -R --binary "$PATCH"; then
  echo "[solve] oracle patch is not cleanly reversible on the base tree" >&2
  exit 1
fi

expected_paths=$(printf '%s\n' \
  lib/index.js \
  lib/processMultipart.js \
  lib/tempFileHandler.js | LC_ALL=C sort)
actual_paths=$(git diff --cached --name-only | LC_ALL=C sort)
if [ "$actual_paths" != "$expected_paths" ]; then
  echo "[solve] oracle patch changed unexpected paths:" >&2
  printf '%s\n' "$actual_paths" >&2
  exit 1
fi

git commit -m "Migrate express-fileupload to Koa 2 middleware"
echo "[solve] applied Koa 2 middleware migration on $(git rev-parse --abbrev-ref HEAD)"

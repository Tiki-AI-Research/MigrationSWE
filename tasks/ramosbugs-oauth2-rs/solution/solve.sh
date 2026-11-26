#!/bin/sh
set -eu
patch="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/solution.patch"
repository=$(git rev-parse --show-toplevel)
cd "$repository"
expected="72ce74401c26eb4dc85dcbfde587bbcfc149e3ae"
actual=$(git rev-parse HEAD)
if [ "$actual" != "$expected" ]; then
  echo "solve.sh requires baseline $expected, got $actual" >&2
  exit 2
fi
# The environment ships the Go export-surface skeleton as untracked files
# (environment/skeleton/ -> /workspace/repo). Those exact paths are part of the
# pristine baseline for this task, so ignore them here; any *other* untracked
# file is still a hard error.
untracked=$(git ls-files --others --exclude-standard | grep -vE '^(go\.mod|oauth2/[A-Za-z0-9_]+\.go)$' || true)
if ! git diff --quiet --ignore-submodules=none -- || ! git diff --cached --quiet --ignore-submodules=none -- || [ -n "$untracked" ]; then
  echo "solve.sh requires a clean baseline worktree and index" >&2
  exit 2
fi
git apply --check --binary "$patch"
git apply --binary "$patch"

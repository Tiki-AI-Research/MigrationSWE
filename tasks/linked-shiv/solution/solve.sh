#!/bin/sh
set -eu
REPO="/workspace/repo"
PATCH_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
expected="ff542cbe75ea832df3a989d07c7fdf5214f727fa"
actual="$(git -C "$REPO" rev-parse HEAD)"
if [ "$actual" != "$expected" ]; then
  echo "solve.sh requires baseline $expected, got $actual" >&2
  exit 2
fi
git -C "$REPO" switch -c shiv-click-8.1.7
git -C "$REPO" apply --binary "$PATCH_DIR/solution.patch"
git -C "$REPO" add -A
# The candidate image ships no git identity; set a repo-local fallback so the
# reference solution can be committed the same way as the golden convention.
if ! git -C "$REPO" config --get user.email >/dev/null 2>&1; then
  git -C "$REPO" config user.email "oracle@harbor.local"
fi
if ! git -C "$REPO" config --get user.name >/dev/null 2>&1; then
  git -C "$REPO" config user.name "Harbor Oracle"
fi
git -C "$REPO" commit -m "solution: migrate shiv from Click 6.7 to 8.1.7"

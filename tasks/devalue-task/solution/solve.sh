#!/bin/sh
set -eu
expected="3ba57a6a8e60b171f624b6343be704b23a34af6e"
actual=$(git rev-parse HEAD)
if [ "$actual" != "$expected" ]; then
  echo "solve.sh requires baseline $expected, got $actual" >&2
  exit 2
fi
git apply --binary "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/solution.patch"
git add -A
if ! git diff --cached --quiet; then
  git -c user.name="migration-oracle" -c user.email="oracle@example.invalid" \
    commit -m "apply reference migration solution" >/dev/null
fi

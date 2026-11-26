#!/bin/sh
set -eu
patch="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/solution.patch"
repository=$(git rev-parse --show-toplevel)
cd "$repository"
expected="68880f87a26e56a36965290109e044b598dec141"
actual=$(git rev-parse HEAD)
if [ "$actual" != "$expected" ]; then
  echo "solve.sh requires baseline $expected, got $actual" >&2
  exit 2
fi
untracked=$(git ls-files --others --exclude-standard)
if ! git diff --quiet --ignore-submodules=none -- || ! git diff --cached --quiet --ignore-submodules=none -- || [ -n "$untracked" ]; then
  echo "solve.sh requires a clean baseline worktree and index" >&2
  exit 2
fi
git apply --check --binary "$patch"
git apply --binary "$patch"

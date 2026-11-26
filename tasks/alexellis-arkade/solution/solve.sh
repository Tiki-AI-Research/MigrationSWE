#!/bin/sh
set -eu
patch="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/solution.patch"
repository=$(git rev-parse --show-toplevel)
cd "$repository"
expected="0a0a800fd7554d4eddb1856f9ef8a21214e95bab"
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
git switch -c migrate-cli-to-kong "$expected"
git apply --binary "$patch"
git add -A -- .
git -c user.name='Migration Solution' -c user.email='migration@example.invalid' \
  commit -m 'Migrate arkade CLI from Cobra to Kong'

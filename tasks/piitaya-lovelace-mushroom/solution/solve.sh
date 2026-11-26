#!/bin/sh
set -eu
patch="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/solution.patch"
repository=$(git rev-parse --show-toplevel)
cd "$repository"
expected="d8a90ffff1b217fbbf9cb8210ee672be99596231"
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
git switch -c migrate-config-zod-reference "$expected"
git apply --binary "$patch"
npm ci --offline --no-audit --no-fund
npm run build
# Build output is outside the requested source migration.
if git ls-files --error-unmatch dist/mushroom.js >/dev/null 2>&1; then
  git restore --worktree -- dist/mushroom.js
fi
git add -A -- .
git -c user.name='Reference Solution' -c user.email='reference@example.invalid' \
  commit -m 'Migrate configuration validation from Superstruct to Zod'

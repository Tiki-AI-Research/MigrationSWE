#!/bin/sh
set -eu
patch="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/solution.patch"
repository=$(git rev-parse --show-toplevel)
cd "$repository"
expected="dfe3b2f09b0200a21481e51e9a9b240376d09061"
actual=$(git rev-parse HEAD)
if [ "$actual" != "$expected" ]; then
  echo "solve.sh requires the instruction's baseline $expected, got $actual; restore the original Git object before running the reference solution" >&2
  exit 2
fi
untracked=$(git ls-files --others --exclude-standard)
if ! git diff --quiet --ignore-submodules=none -- || ! git diff --cached --quiet --ignore-submodules=none -- || [ -n "$untracked" ]; then
  echo "solve.sh requires a clean baseline worktree and index" >&2
  exit 2
fi
git apply --check --binary "$patch"
git switch -c migrate-hono-reference "$expected"
git apply --binary "$patch"
if ! npm ls hono @hono/node-server --depth=0 >/dev/null 2>&1; then
  npm ci --no-audit --no-fund
fi
npm test -- --testTimeout=30000
git add -A
git -c user.name="Harbor Oracle" -c user.email="oracle@example.invalid" \
  commit -m "Migrate the backend HTTP layer from Express to Hono"

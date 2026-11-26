#!/bin/sh
set -eu
patch="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/solution.patch"
repository=$(git rev-parse --show-toplevel)
cd "$repository"
expected="ee18073a11e04534911bd2900e9879ababed3367"
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
git switch -c "omg-python-migration-$(date +%s)-$$" "$expected"
git apply --binary "$patch"
git add -- mcp-server
git -c user.name="Harbor Oracle" -c user.email="oracle@localhost" commit -m "Migrate OMG MCP server to Python"

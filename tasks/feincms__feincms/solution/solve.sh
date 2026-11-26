#!/bin/sh
set -eu
patch="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/solution.patch"
repository=$(git rev-parse --show-toplevel)
cd "$repository"
expected="060eb148192aaec6aab394cf3001f1e5309bc548"
actual=$(git rev-parse HEAD)
if [ "$actual" != "$expected" ]; then
  echo "solve.sh requires baseline $expected, got $actual" >&2
  exit 2
fi
# The image may supply untracked environment helpers such as .venv. Preserve
# them; git apply --check below still rejects collisions with solution files.
if ! git diff --quiet --ignore-submodules=none -- || ! git diff --cached --quiet --ignore-submodules=none --; then
  echo "solve.sh requires unchanged tracked baseline files and index" >&2
  exit 2
fi
git apply --check --binary "$patch"
git apply --binary "$patch"

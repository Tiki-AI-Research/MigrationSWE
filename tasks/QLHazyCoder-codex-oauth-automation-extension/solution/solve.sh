#!/bin/sh
set -eu
patch="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/solution.patch"
repository=$(git rev-parse --show-toplevel)
cd "$repository"
expected="bf6049f196278eba0f1563f190c3934453fa548f"
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
git switch -c migrate-mail-helpers-flask "$expected"
git apply --binary "$patch"
git add -- scripts/hotmail_helper.py scripts/custom_mail_helper.py requirements.txt
git -c user.name='Oracle' -c user.email='oracle@example.invalid' commit -m 'Migrate local mail helpers to Flask 3.1.2'
# Commit all changes even if the unrelated extension suite hits a timer flake.
# Run the exact public command first; a retry still runs every assertion, with
# fewer concurrent files to avoid scheduling-sensitive failures under load.
if node --test tests/*.test.js; then
  exit 0
fi
echo 'Public suite failed; rerunning all tests with one file at a time' >&2
node --test --test-concurrency=1 tests/*.test.js

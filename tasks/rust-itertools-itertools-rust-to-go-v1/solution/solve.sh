#!/bin/sh
set -eu
patch="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/solution.patch"
repository=$(git rev-parse --show-toplevel)
cd "$repository"
expected="fd619332f002979c3d7958fe0fc45509f84fdb8d"
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
git switch -c go-migration
git apply --binary "$patch"
export GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local CGO_ENABLED=0
go build ./...
go test ./...
go build -tags nostd ./...
go test -tags nostd ./...
go build -tags noalloc ./...
go test -tags noalloc ./...
git add -- '*.go' core free go.mod Makefile
git -c user.name='Harbor Oracle' -c user.email='oracle@example.invalid' commit -m 'Migrate itertools to Go with iterator behavior and tests'

#!/bin/sh
# Executable P2P projection for BATCH-P2P-0002 (target side).
# Runs the repository's own Go tests for the injected projection package with an
# explicit timeout so the harness can never wait forever. The exit status is the
# raw PASS/FAIL of this side.
set -eu

pkg_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$pkg_dir
while [ ! -f "$repo_root/go.mod" ]; do
  parent=$(dirname -- "$repo_root")
  if [ "$parent" = "$repo_root" ]; then
    break
  fi
  repo_root=$parent
done

pkg_rel=${pkg_dir#"$repo_root"/}

cd "$repo_root"
exec timeout 300 go test -count=1 -timeout 240s "./$pkg_rel/..."

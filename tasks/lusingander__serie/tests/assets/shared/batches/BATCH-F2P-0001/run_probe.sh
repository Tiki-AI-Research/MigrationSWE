#!/bin/sh
set -eu

repo=${1:-}
observation=${2:-}

if [ -z "$repo" ] || [ -z "$observation" ]; then
  echo '{"observation":"","status":"INFRA_ERROR","detail":"usage: run_probe.sh <repository-path> <observation-id>"}'
  exit 2
fi

if [ ! -d "$repo" ]; then
  printf '{"observation":"%s","status":"FAIL","detail":"repository path is absent"}\n' "$observation"
  exit 1
fi
repo=$(CDPATH= cd -- "$repo" && pwd)

if [ ! -f "$repo/go.mod" ] || [ ! -d "$repo/core" ]; then
  printf '{"observation":"%s","status":"FAIL","detail":"target Go module or core package is absent"}\n' "$observation"
  exit 1
fi

go_bin=$(command -v go 2>/dev/null || true)
if [ -z "$go_bin" ]; then
  printf '{"observation":"%s","status":"INFRA_ERROR","detail":"go toolchain is unavailable"}\n' "$observation"
  exit 2
fi

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

cp "$here/probe.go" "$tmp/probe.go"
cat > "$tmp/go.mod" <<EOF
module serie-f2p-probe

go 1.24.0

require github.com/lusingander/serie v0.0.0

replace github.com/lusingander/serie => $repo
EOF

export GOCACHE="$tmp/gocache"
export GOMODCACHE="$tmp/gomodcache"
export GOFLAGS=-mod=mod
export GOPROXY=off
export GOSUMDB=off
export GOTOOLCHAIN=local
export CGO_ENABLED=0

if ! (cd "$tmp" && "$go_bin" build -buildvcs=false -o probe .) >"$tmp/build.log" 2>&1; then
  printf '{"observation":"%s","status":"FAIL","detail":"target Go core could not be loaded and built"}\n' "$observation"
  exit 1
fi

"$tmp/probe" "$observation" "$repo"

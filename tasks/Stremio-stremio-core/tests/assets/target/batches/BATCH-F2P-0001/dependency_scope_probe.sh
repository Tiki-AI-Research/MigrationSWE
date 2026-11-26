#!/bin/sh
# BATCH-F2P-0001 target-side dependency/scope probe.
#
# Usage: dependency_scope_probe.sh [repo_dir]
#
# Verifies the target frozen-contract postconditions that are observable from
# the build graph and the source tree (observations F2P-001, F2P-002, F2P-004,
# F2P-011, F2P-016, F2P-017, F2P-020, F2P-021):
#   * the Go module builds standalone with CGO disabled,
#   * go list -deps only resolves the module and the standard library,
#   * no production Go file references cgo / FFI / subprocess / RPC / dlopen,
#   * the out-of-scope stremio-core-web tree carries no migrated Go/wasm output.
# Exits 0 on success, non-zero on the first violated postcondition.
set -eu

REPO="${1:-/workspace/repo}"
cd "$REPO"

fail() {
	echo "F2P-PROBE FAIL: $1" >&2
	exit 1
}

[ -f go.mod ] || fail "go.mod missing; not a Go module"
grep -q '^module github.com/stremio/stremio-core$' go.mod \
	|| fail "unexpected module path in go.mod"

# 1. Standalone build with cgo disabled.
CGO_ENABLED=0 go build ./... || fail "CGO_ENABLED=0 go build ./... failed"

# 2. The build graph resolves only the module and the standard library.
deps=$(CGO_ENABLED=0 go list -deps ./...)
for dep in $deps; do
	case "$dep" in
	github.com/stremio/stremio-core*) ;;
	*.*/*|*.*) fail "non-standard dependency resolved: $dep" ;;
	esac
done

# 3. No cgo / FFI / subprocess / RPC / dynamic-load delegation in production Go.
if grep -RIn --include='*.go' -e 'import "C"' -e '//go:cgo' -e '#cgo' . >/dev/null 2>&1; then
	fail "cgo directive/import found in Go sources"
fi
for forbidden in '"os/exec"' '"plugin"' '"net/rpc"' '"net/rpc/jsonrpc"' '"runtime/cgo"'; do
	if grep -RIn --include='*.go' -F "$forbidden" . | grep -v '_test.go' >/dev/null 2>&1; then
		fail "delegation import $forbidden found in production Go"
	fi
done

# 4. The out-of-scope web/wasm tree must not have been migrated.
if find stremio-core-web -type f \( -name '*.go' -o -name '*.wasm' \) 2>/dev/null | grep -q .; then
	fail "stremio-core-web contains migrated Go/wasm output"
fi

echo "F2P-PROBE PASS: standalone Go module, no Rust delegation"
exit 0

#!/usr/bin/env bash
# Differential target probe harness for BATCH-DIFFERENTIAL-0001.
#
# Copies the migrated Go repository to a scratch directory, installs the
# batch's probe tests, and runs them with explicit timeouts. Every potentially
# long-running command is wrapped in `timeout`; the harness never waits
# indefinitely and never requires privilege.
#
# Applicability contract
# ----------------------
# The probe tests exercise target *internal* packages through their canonical
# reference import paths.  A candidate's internal module path / package layout
# / exported names are NOT part of the declared public contract, so the probe
# is only meaningful for a candidate that happens to keep the reference's
# internal structure.  To avoid turning that undeclared structure into a hidden
# contract:
#
#   * the candidate's own module path is resolved from `go.mod` and injected
#     into the probe imports, so any module name works; and
#   * if the probe still cannot be *compiled* against the candidate (different
#     package layout or exported names), the harness exits with code 3 to mean
#     "not applicable".  The caller maps that to `NA`, never to a behavioural
#     `FAIL`.
#
# Exit codes: 0 = probe compiled and all differential assertions passed;
# 1 = probe compiled but a differential assertion failed (behavioural);
# 3 = probe not applicable to this candidate's internal structure.
set -euo pipefail

TARGET="${1:-/input/target}"
HERE="$(cd "$(dirname "$0")" && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/repo"
cp -a "$TARGET/." "$WORK/repo/"
rm -rf "$WORK/repo/.git"

export GOFLAGS="${GOFLAGS:--mod=vendor}"
export GOCACHE="${GOCACHE:-$WORK/gocache}"
export GOTOOLCHAIN="${GOTOOLCHAIN:-local}"
export GOPROXY="${GOPROXY:-off}"

# Resolve the candidate's module path from its own go.mod (never hardcoded).
MODULE="$(awk '/^module[[:space:]]+/{print $2; exit}' "$WORK/repo/go.mod" 2>/dev/null || true)"
if [ -z "$MODULE" ]; then
  MODULE="github.com/huseyinbabal/taws"
fi

mkdir -p "$WORK/repo/differentialprobe"
sed "s#github.com/huseyinbabal/taws#${MODULE}#g" \
  "$HERE/differential_probe_test.go" > "$WORK/repo/differentialprobe/differential_probe_test.go"
sed "s#github.com/huseyinbabal/taws#${MODULE}#g" \
  "$HERE/ssm_probe_test.go" > "$WORK/repo/ssm_probe_test.go"

cd "$WORK/repo"

timeout 600 go build ./...

# Compile-only applicability check.  A build/test-compile failure here means
# the probe cannot be applied to the candidate's internal structure; report
# "not applicable" (exit 3) instead of a behavioural failure.
if ! timeout 600 go test -run '^$' -count=1 ./differentialprobe/ . >"$WORK/compile.log" 2>&1; then
  echo "differential probe not applicable to candidate internal structure (compile failed):" >&2
  tail -n 20 "$WORK/compile.log" >&2 || true
  exit 3
fi

timeout 600 go test ./differentialprobe/ . -count=1

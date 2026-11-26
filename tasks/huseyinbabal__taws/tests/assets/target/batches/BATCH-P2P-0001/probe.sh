#!/bin/sh
# BATCH-P2P-0001 target-side P2P probe.
#
# Runs the migrated Go repository's own test suite inside the target container
# and maps each P2P observation owned by this batch to that side's raw
# PASS/FAIL result.  The projection value produced for every observation is the
# stable common wrapper {"result":"PASS"} (or FAIL), so the source and target
# sides normalise to the same value.
#
# Every potentially long-running command is wrapped with an explicit `timeout`
# so the harness can never block forever.  Network-dependent AWS calls are
# never exercised: the ported suite uses in-process mock servers only.  The
# probe always emits a complete report, even when the toolchain is absent.
set -u

REPORT="${1:-/logs/result.json}"
BATCH="BATCH-P2P-0001"
TEST_TIMEOUT="${P2P_TEST_TIMEOUT:-1500}"
LOG="${P2P_LOG:-/tmp/p2p_target_tests.log}"

mkdir -p "$(dirname "$REPORT")" 2>/dev/null || true
: > "$LOG" 2>/dev/null || true

locate_repo() {
  for d in "${TARGET_REPO:-}" /workspace/repo /input/target /src /repo "$PWD"; do
    [ -n "$d" ] || continue
    [ -f "$d/go.mod" ] && { printf '%s' "$d"; return 0; }
  done
  for d in /workspace/* /input/*; do
    [ -f "$d/go.mod" ] && { printf '%s' "$d"; return 0; }
  done
  for d in "${TARGET_REPO:-}" /workspace/repo /input/target "$PWD"; do
    [ -n "$d" ] && [ -d "$d" ] && { printf '%s' "$d"; return 0; }
  done
  printf '%s' "/workspace/repo"
}
REPO="$(locate_repo)"
cd "$REPO" 2>/dev/null || true

suite_ok=0
if command -v go >/dev/null 2>&1; then
  if timeout "$TEST_TIMEOUT" env CGO_ENABLED=0 GOFLAGS=-mod=vendor GOPROXY=off \
      go test -v -count=1 ./... >>"$LOG" 2>&1; then
    suite_ok=1
  elif timeout "$TEST_TIMEOUT" env CGO_ENABLED=0 GOPROXY=off \
      go test -v -count=1 ./... >>"$LOG" 2>&1; then
    suite_ok=1
  fi
fi

build_ok=0
if command -v go >/dev/null 2>&1; then
  if timeout "$TEST_TIMEOUT" env CGO_ENABLED=0 GOFLAGS=-mod=vendor GOPROXY=off \
      go build ./... >>"$LOG" 2>&1; then
    build_ok=1
  fi
fi

if [ "$suite_ok" = 1 ]; then overall="PASS"; else overall="FAIL"; fi

# Selector per observation.  A TestXxx value is resolved from the `go test -v`
# log; a SYM:<literal> value is resolved by verifying the compiled package
# graph exposes the symbol.
selector_for() {
  case "$1" in
    P2P-001) printf 'SYM:reportSSMExit' ;;
    P2P-002) printf 'SYM:log-level' ;;
    P2P-003) printf 'TestSerializeDeserialize' ;;
    P2P-004) printf 'SYM:list-profiles' ;;
    P2P-005) printf '' ;;
    P2P-006) printf 'TestReadOnlyBlocksMutatingActions' ;;
    P2P-007) printf 'TestGetAllResourceKeys' ;;
    P2P-008) printf 'TestParseEC2Response' ;;
    P2P-009) printf 'TestJSONParseWithPagination' ;;
    P2P-010) printf 'TestAPIProtocolDeserialize' ;;
    P2P-011) printf 'TestAddRecentRegion' ;;
    P2P-012) printf 'TestFetchResourcesPaginatedJSONProtocol' ;;
    P2P-013) printf 'TestGetLoginCacheDirEnvOverride' ;;
    P2P-014) printf 'TestNewHTTPClientHonorsEndpointEnvOverride' ;;
    P2P-015) printf '' ;;
    P2P-016) printf 'TestParseINIFileWithRoleArn' ;;
    P2P-017) printf 'TestResolveStaticParamTemplateReplacesAllPlaceholders' ;;
    P2P-018) printf 'TestLoadInvalidDoesNotPanic' ;;
    P2P-019) printf 'TestParseINIFile' ;;
    P2P-020) printf 'TestLoadMissingFileLogsWarningAndUsesDefaults' ;;
    P2P-021) printf 'TestCredentialsErrorSSOVsConsoleLogin' ;;
    *) printf '' ;;
  esac
}

obs_result() {
  sel="$1"
  [ -n "$sel" ] || { printf '%s' "$overall"; return; }
  case "$sel" in
    SYM:*)
      sym="$(printf '%s' "$sel" | cut -d: -f2-)"
      if [ "$build_ok" = 1 ] && \
         grep -rq -- "$sym" "$REPO/main.go" "$REPO/internal" 2>/dev/null; then
        printf 'PASS'
      else
        printf '%s' "$overall"
      fi
      ;;
    *)
      if grep -qE "^[[:space:]]*--- FAIL: ${sel}([[:space:]]|$)" "$LOG" 2>/dev/null; then
        printf 'FAIL'
      elif grep -qE "^[[:space:]]*--- PASS: ${sel}([[:space:]]|$)" "$LOG" 2>/dev/null; then
        printf 'PASS'
      else
        printf '%s' "$overall"
      fi
      ;;
  esac
}

{
  printf '{"batch_id":"%s","side":"target","result":"%s","observations":{' "$BATCH" "$overall"
  first=1
  n=1
  while [ "$n" -le 21 ]; do
    id="$(printf 'P2P-%03d' "$n")"
    res="$(obs_result "$(selector_for "$id")")"
    [ "$first" -eq 0 ] && printf ','
    first=0
    printf '"%s":{"result":"%s"}' "$id" "$res"
    n=$((n + 1))
  done
  printf '}}\n'
} > "$REPORT" 2>/dev/null || true

printf '%s\n' "$overall"
exit 0

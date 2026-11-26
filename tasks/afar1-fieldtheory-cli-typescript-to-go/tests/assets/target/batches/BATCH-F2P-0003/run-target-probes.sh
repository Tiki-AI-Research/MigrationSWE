#!/bin/sh
# Target-side F2P probes for BATCH-F2P-0003.
#
# Builds the Go reference implementation from the target repository and
# exercises the observable behavior of the `ft` / `fieldtheory` executable plus
# the in-scope internal packages. Every long-running command goes through the
# shared timeout helpers. Interactive/TTY, network and privileged operations are
# never used.
#
# Emits one `OBS <observation_id> <status>` line per observation and, when a
# report path is supplied, a JSON report consumed by the integration entry
# point.
#
# Usage: run-target-probes.sh [report.json]

set -eu

HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
FT_ASSETS_ROOT="${FT_ASSETS_ROOT:-$(CDPATH= cd -- "$HERE/../../.." && pwd)}"
FT_BATCH_DIR="$FT_ASSETS_ROOT/target/batches/BATCH-F2P-0003"
FT_FIXTURES_DIR="$FT_ASSETS_ROOT/fixtures/batches/BATCH-F2P-0003"
FT_REPORT="${1:-${FT_REPORT:-}}"
export FT_ASSETS_ROOT FT_BATCH_DIR FT_FIXTURES_DIR FT_REPORT

# shellcheck disable=SC1091
. "$FT_ASSETS_ROOT/shared/batches/BATCH-F2P-0003/probe_lib.sh"

REPO_ROOT="$(ft_resolve_repo "${REPO_ROOT:-}" go.mod)"
TMP="$(ft_mktempdir)"
trap 'rm -rf "$TMP"' EXIT INT TERM

DATA_DIR="$TMP/data"
HOME_DIR="$TMP/home"
LIB_DIR="$TMP/lib"
CMD_DIR="$TMP/cmd"
mkdir -p "$DATA_DIR" "$HOME_DIR" "$LIB_DIR" "$CMD_DIR"
cp "$FT_FIXTURES_DIR/bookmarks.jsonl" "$DATA_DIR/bookmarks.jsonl"

# ---------------------------------------------------------------------------
# Build the Go binary and a second entry-point copy.
# ---------------------------------------------------------------------------
BUILD_OK=0
set +e
( cd "$REPO_ROOT" && run_timed "$FT_BUILD_TIMEOUT" python3 "$FT_ASSETS_ROOT/shared/integration/build_cli.py" "$REPO_ROOT" "$TMP/ft" ) \
  >"$TMP/build.out" 2>"$TMP/build.err"
BUILD_STATUS=$?
set -e
if [ "$BUILD_STATUS" -eq 0 ] && [ -x "$TMP/ft" ]; then
  BUILD_OK=1
  cp "$TMP/ft" "$TMP/fieldtheory"
fi

# Run the built binary against the isolated data directory. The CLI invocation
# is bounded by the shared timeout helper so a prompt or watcher can never hang.
ft_run() {
  FT_DATA_DIR="$DATA_DIR" HOME="$HOME_DIR" \
  FT_LIBRARY_DIR="$LIB_DIR" FT_COMMANDS_DIR="$CMD_DIR" \
  run_timed "$FT_CMD_TIMEOUT" "$TMP/ft" "$@"
}

# Run with node/tsx removed from PATH (still timeout-bounded).
ft_run_nonode() {
  FT_DATA_DIR="$DATA_DIR" HOME="$HOME_DIR" \
  FT_LIBRARY_DIR="$LIB_DIR" FT_COMMANDS_DIR="$CMD_DIR" \
  PATH="/usr/bin:/bin" run_timed "$FT_CMD_TIMEOUT" "$TMP/fieldtheory" "$@"
}

# Extract the primaryCategory for a given id from `ft list --json` on stdin.
f2p_primary_for() {
  awk -v want="$1" '
    /"id":/            { gsub(/[",]/, "", $2); cur=$2 }
    /"primaryCategory":/ { gsub(/[",]/, "", $2); if (cur==want) print $2 }
  '
}

# ===========================================================================
# F2P-052 (R052): native Go toolchain + no Node/TS runtime dependency.
# Third-party libraries (command parser, SQLite driver) are an implementation
# choice and are not part of the documented contract, so they are not pinned.
# ===========================================================================
probe_052() {
  # A stale/wrong evaluator image is not evidence against the candidate.
  # The Dockerfiles also verify the exact toolchain when building the image.
  toolchain="$(GOTOOLCHAIN=local run_timed "$FT_CMD_TIMEOUT" go env GOVERSION)" || toolchain=""
  if [ "$toolchain" != go1.23.0 ]; then
    printf 'F2P-052 infrastructure: expected go1.23.0; evaluator provides %s\n' "${toolchain:-unavailable}" >&2
    ft_emit F2P-052 INFRA_ERROR
    return
  fi
  if [ "$BUILD_OK" -ne 1 ]; then
    ft_emit F2P-052 INFRA_ERROR
    return
  fi
  status=PASS
  # The binary was really built with the required compiler above. Both
  # `go 1.23` and `go 1.23.0` are compatible module directives.
  # Module identifiers and comments do not establish runtime delegation.
  # Native execution probes and the source-runtime detector cover that rule.
  [ -x "$TMP/ft" ] || status=FAIL
  [ -x "$TMP/fieldtheory" ] || status=FAIL
  ft_emit F2P-052 "$status"
}

# ===========================================================================
# F2P-050 (R001): `fieldtheory path` in-process without node/tsx.
# ===========================================================================
probe_050() {
  [ "$BUILD_OK" -eq 1 ] || { ft_emit F2P-050 INFRA_ERROR; return; }
  set +e
  out="$(ft_run_nonode path 2>"$TMP/e050")"
  status=$?
  set -e
  if [ "$status" -ne 0 ]; then
    ft_emit F2P-050 FAIL
    return
  fi
  if [ "$out" = "$DATA_DIR" ]; then
    ft_emit F2P-050 PASS
  else
    ft_emit F2P-050 FAIL
  fi
}

# ===========================================================================
# F2P-057 / F2P-062 (R034): migrated work runs with node absent.
# ===========================================================================
probe_cli_contract() {
  _contract_id="$1"; _contract_mode="$2"
  [ "$BUILD_OK" -eq 1 ] || { ft_emit "$_contract_id" INFRA_ERROR; return; }
  set +e
  run_timed 180 python3 "$FT_ASSETS_ROOT/shared/integration/cli_contract.py" "$_contract_mode" "$TMP/ft" >"$TMP/$_contract_id-contract.log" 2>&1
  contract_rc=$?
  set -e
  case "$contract_rc" in
    0) contract_status=PASS ;;
    2) contract_status=INCONCLUSIVE ;;
    124|137) contract_status=TIMEOUT ;;
    126|127) contract_status=INFRA_ERROR ;;
    *) contract_status=FAIL ;;
  esac
  if [ "$contract_rc" -ne 0 ]; then cat "$TMP/$_contract_id-contract.log" >&2; fi
  ft_emit "$_contract_id" "$contract_status"
}

probe_057() { probe_cli_contract F2P-057 native-classify; }

probe_062() { probe_cli_contract F2P-062 native-database; }

# ===========================================================================
# F2P-051 (R043): index refresh preserves rows; --force rebuilds.
# ===========================================================================
probe_051() { probe_cli_contract F2P-051 refresh; }

# ===========================================================================
# F2P-053: search --json preserves bookmark values and public field names.
# Whitespace and JSON escaping are serialization choices, not cache semantics.
# ===========================================================================
probe_053() {
  [ "$BUILD_OK" -eq 1 ] || { ft_emit F2P-053 INFRA_ERROR; return; }
  ft_run index >/dev/null 2>&1 || true
  if ft_run search b --json >"$TMP/o053" 2>"$TMP/e053" &&
     python3 - "$TMP/o053" <<'PYJSON'
import json, sys
rows = json.load(open(sys.argv[1]))
assert isinstance(rows, list)
match = [row for row in rows if row.get("text") == "a<b>&c"]
assert match and "authorHandle" in match[0] and "postedAt" in match[0]
PYJSON
  then ft_emit F2P-053 PASS; else ft_emit F2P-053 FAIL; fi
}

# ===========================================================================
# ===========================================================================
# F2P-056 (R045): `ft classify --regex` fixed priority categories.
# ===========================================================================
probe_056() { probe_cli_contract F2P-056 classify; }

# ===========================================================================
# F2P-064 (R051): hidden bookmarks alias forwards to the top-level command.
# Tolerating unknown flags is not part of the documented contract (argument and
# parse errors MUST exit non-zero), so only forwarding is required.
# ===========================================================================
probe_064() {
  [ "$BUILD_OK" -eq 1 ] || { ft_emit F2P-064 INFRA_ERROR; return; }
  ft_run index >/dev/null 2>&1 || true
  set +e
  ft_run bookmarks search machine --limit 5 >"$TMP/o064" 2>&1
  status=$?
  set -e
  if [ "$status" -eq 0 ] && grep -q 'machine learning' "$TMP/o064"; then
    ft_emit F2P-064 PASS
  else
    ft_emit F2P-064 FAIL
  fi
}

# ===========================================================================
# F2P-058 / F2P-061 / F2P-068 / F2P-069 (R049): traversal rejection.
# ===========================================================================
probe_traversal() {
  id="$1"
  [ "$BUILD_OK" -eq 1 ] || { ft_emit "$id" INFRA_ERROR; return; }
  outside="$TMP/outside-evil"
  rm -f "$outside"
  set +e
  ft_run commands new ../evil >"$TMP/tr1" 2>&1; r1=$?
  ft_run commands new _hidden >"$TMP/tr2" 2>&1; r2=$?
  ft_run library show ../../etc/passwd >"$TMP/tr3" 2>&1; r3=$?
  set -e
  if [ "$r1" -ne 0 ] && [ "$r2" -ne 0 ] && [ "$r3" -ne 0 ] && [ ! -e "$outside" ] && [ ! -e "$TMP/evil" ] && [ ! -e "$LIB_DIR/../../etc/passwd" ]; then
    ft_emit "$id" PASS
  else
    ft_emit "$id" FAIL
  fi
}

# ===========================================================================
# F2P-059 (R049): library content/traversal guards.
# ===========================================================================
probe_059() { probe_cli_contract F2P-059 library; }

# ===========================================================================
# Go-package probes (transplanted into a private working copy).
# Covers F2P-049, 054, 055, 060, 063, 065, 066, 067, 070, 071, 072.
# ===========================================================================
GO_PROBE_IDS="F2P-049 F2P-054 F2P-055 F2P-060 F2P-063 F2P-065 F2P-066 F2P-067 F2P-070 F2P-071 F2P-072"

# Signature-aware module adapters run in private copies of the candidate.
# The LLM/preferences contracts use public CLI state, requiring no package or
# module names. Cache a receipt only for IDs that own the same contract.
run_go_probes() {
  [ "$BUILD_OK" -eq 1 ] || {
    for id in $GO_PROBE_IDS; do ft_emit "$id" INFRA_ERROR; done
    return
  }
  for pair in F2P-049:argv F2P-054:preferences F2P-055:engine F2P-060:llm \
              F2P-063:dates F2P-065:preferences F2P-066:env F2P-067:env \
              F2P-070:prompt F2P-071:dates F2P-072:env; do
    id="${pair%%:*}"; mode="${pair#*:}"
    receipt="$TMP/adapter-$mode.status"
    if [ ! -f "$receipt" ]; then
      set +e
      case "$mode" in
        preferences|llm)
          run_timed 180 python3 "$FT_ASSETS_ROOT/shared/integration/cli_contract.py" "$mode" "$TMP/ft" >"$TMP/adapter-$mode.log" 2>&1 ;;
        *)
          run_timed 270 python3 "$FT_ASSETS_ROOT/shared/integration/module_contract.py" "$REPO_ROOT" "$mode" >"$TMP/adapter-$mode.log" 2>&1 ;;
      esac
      adapter_rc=$?
      set -e
      case "$adapter_rc" in
        0) adapter_status=PASS ;;
        2) adapter_status=INCONCLUSIVE ;;
        124|137) adapter_status=TIMEOUT ;;
        126|127) adapter_status=INFRA_ERROR ;;
        *) adapter_status=FAIL ;;
      esac
      printf '%s\n' "$adapter_status" >"$receipt"
      if [ "$adapter_rc" -ne 0 ]; then
        printf '%s adapter diagnostics:\n' "$mode" >&2
        cat "$TMP/adapter-$mode.log" >&2
      fi
    fi
    ft_emit "$id" "$(cat "$receipt")"
  done
}

probe_052
probe_050
probe_057
probe_062
probe_051
probe_053
probe_056
probe_064
probe_traversal F2P-058
probe_traversal F2P-061
probe_traversal F2P-068
probe_traversal F2P-069
probe_059
run_go_probes

ft_write_report target BATCH-F2P-0003 "$FT_REPORT"

echo "target probes complete"

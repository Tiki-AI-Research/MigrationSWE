#!/bin/sh
# Target-side probes for BATCH-F2P-0001 (TypeScript -> Go migration).
#
# Builds the Go `ft`/`fieldtheory` binary from the target repository, exercises
# the migrated responsibilities, overlays the F2P Go probe tests into a writable
# copy, and emits one JSON result object per observation on stdout (and into
# $FT_RESULTS_FILE when set). Every long-running command is wrapped by the
# shared timeout helpers; nothing waits indefinitely.

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ASSETS_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)

# shellcheck source=/dev/null
. "$ASSETS_ROOT/shared/batches/BATCH-F2P-0001/probe-lib.sh"
ft_load_timeout "$ASSETS_ROOT"
set +e

REPO_ROOT=$(ft_target_repo)
FIXTURES="$ASSETS_ROOT/fixtures/batches/BATCH-F2P-0001"
PROBES="$SCRIPT_DIR/probes"

WORK=$(mktemp -d)
FT_RESULTS_FILE="$WORK/results.jsonl"
: >"$FT_RESULTS_FILE"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

BIN="$WORK/bin/ft"
BIN_ALT="$WORK/bin/fieldtheory"
DATA="$WORK/data"
NODE_ABSENT=0
ft_node_absent && NODE_ABSENT=1

mkdir -p "$WORK/bin" "$DATA"
cp "$FIXTURES/bookmarks.jsonl" "$DATA/bookmarks.jsonl"

# --- build the native binaries -------------------------------------------------
BUILD_OK=0
if ( cd "$REPO_ROOT" && run_timed 300 go build -o "$BIN" ./cmd/ft ) >"$WORK/build.log" 2>&1; then
  if ( cd "$REPO_ROOT" && run_timed 300 go build -o "$BIN_ALT" ./cmd/ft ) >>"$WORK/build.log" 2>&1; then
    BUILD_OK=1
  fi
fi
ft_note "build_ok=$BUILD_OK node_absent=$NODE_ABSENT"

# index the fixture cache for the CLI probes
if [ "$BUILD_OK" = 1 ]; then
  FT_DATA_DIR="$DATA" run_timed 60 "$BIN" index >/dev/null 2>&1 || true
fi

# --- overlay the Go probe tests into a writable copy ---------------------------
GOTEST_LOG="$WORK/gotest.log"
GOTEST_OK=0
if [ "$BUILD_OK" = 1 ]; then
  OVERLAY="$WORK/overlay"
  if cp -a "$REPO_ROOT" "$OVERLAY" 2>/dev/null; then
    rm -rf "$OVERLAY/.git"
    mkdir -p "$OVERLAY/internal/engine" "$OVERLAY/internal/store" "$OVERLAY/internal/fsutil"
    cp "$PROBES/engine_probe_test.go" "$OVERLAY/internal/engine/zz_f2p_probe_test.go"
    cp "$PROBES/store_probe_test.go" "$OVERLAY/internal/store/zz_f2p_probe_test.go"
    cp "$PROBES/fsutil_probe_test.go" "$OVERLAY/internal/fsutil/zz_f2p_probe_test.go"
    if ( cd "$OVERLAY" && run_timed 600 go test -v ./internal/engine/ ./internal/store/ ./internal/fsutil/ ) >"$GOTEST_LOG" 2>&1; then
      GOTEST_OK=1
    fi
  fi
fi
ft_note "overlay_go_test_ok=$GOTEST_OK"

test_ok() {
  [ "$GOTEST_OK" = 1 ] && grep -q -- "--- PASS: $1" "$GOTEST_LOG"
}

# --- predicate helpers (shared by multiple observations) -----------------------
dep_policy_ok() {
  [ "$NODE_ABSENT" = 1 ] || return 1
  [ "$BUILD_OK" = 1 ] || return 1
  ft_has_js_runtime_imports "$REPO_ROOT" && return 1
  FT_DATA_DIR="$DATA" run_timed 30 "$BIN" path >/dev/null 2>&1
}

same_command_tree_ok() {
  [ "$BUILD_OK" = 1 ] || return 1
  run_timed 30 "$BIN" --help >"$WORK/help_ft.txt" 2>&1 || return 1
  run_timed 30 "$BIN_ALT" --help >"$WORK/help_alt.txt" 2>&1 || return 1
  sed 's/fieldtheory/ft/g' "$WORK/help_alt.txt" | diff - "$WORK/help_ft.txt" >/dev/null 2>&1
}

build_contract_ok() {
  [ "$BUILD_OK" = 1 ] || return 1
  grep -q '^go 1.23.0' "$REPO_ROOT/go.mod" || return 1
  grep -q 'github.com/spf13/cobra' "$REPO_ROOT/go.mod" || return 1
  grep -q 'modernc.org/sqlite' "$REPO_ROOT/go.mod" || return 1
  grep -q './cmd/ft' "$REPO_ROOT/Makefile" || return 1
}

native_binary_ok() {
  [ "$NODE_ABSENT" = 1 ] || return 1
  [ "$BUILD_OK" = 1 ] || return 1
  FT_DATA_DIR="$DATA" run_timed 30 "$BIN" path >/dev/null 2>&1 || return 1
  FT_DATA_DIR="$DATA" run_timed 60 "$BIN" index >/dev/null 2>&1
}

command_surface_ok() {
  [ "$BUILD_OK" = 1 ] || return 1
  run_timed 30 "$BIN" --help 2>&1 | grep -q 'path' || return 1
  run_timed 30 "$BIN" --help 2>&1 | grep -q 'index' || return 1
  run_timed 30 "$BIN" search --help >/dev/null 2>&1 || return 1
  run_timed 30 "$BIN" library --help >/dev/null 2>&1 || return 1
  run_timed 30 "$BIN" commands --help >/dev/null 2>&1
}

in_scope_ok() {
  [ "$NODE_ABSENT" = 1 ] || return 1
  [ "$BUILD_OK" = 1 ] || return 1
  FT_DATA_DIR="$DATA" run_timed 60 "$BIN" index >/dev/null 2>&1 || return 1
  FT_DATA_DIR="$DATA" run_timed 60 "$BIN" classify --regex >/dev/null 2>&1
}

path_surface_ok() {
  [ "$BUILD_OK" = 1 ] || return 1
  run_capture 30 "$WORK/p.out" "$WORK/p.err" env FT_DATA_DIR="$DATA" "$BIN" path || return 1
  [ "$(tr -d ' \n' <"$WORK/p.out")" = "$DATA" ]
}

# -------------------------------------------------------------------------------
relation() { # <obs_id> <0/1>
  if [ "$2" = 1 ]; then ft_emit "$1" pass; else ft_emit "$1" fail; fi
}

# F2P-001 dependency policy through both command names
if dep_policy_ok && same_command_tree_ok; then relation F2P-001 1; else relation F2P-001 0; fi

# F2P-002 make build artifacts / go.mod pins
if build_contract_ok; then relation F2P-002 1; else relation F2P-002 0; fi

# F2P-003 native Go binary runs with node absent
if native_binary_ok; then relation F2P-003 1; else relation F2P-003 0; fi

# F2P-004 engine discovery + argv + label
if test_ok TestF2P004EngineArgvAndLabel && test_ok TestF2P004EngineDiscoveryExecutableBit; then
  relation F2P-004 1
else
  relation F2P-004 0
fi

# F2P-005 production packages carry no JS/Node imports
if dep_policy_ok; then relation F2P-005 1; else relation F2P-005 0; fi

# F2P-006 cobra root command surface
if command_surface_ok; then relation F2P-006 1; else relation F2P-006 0; fi

# F2P-007 index refresh preserves classification (R043)
if test_ok TestF2P043IndexRefreshAndSelfHealing; then relation F2P-007 1; else relation F2P-007 0; fi

# F2P-008 library/commands path confinement
CONF_OK=0
if [ "$BUILD_OK" = 1 ]; then
  CMDS="$WORK/commands"; LIBS="$WORK/library"
  mkdir -p "$CMDS" "$LIBS"
  FT_DATA_DIR="$DATA" FT_COMMANDS_DIR="$CMDS" FT_LIBRARY_DIR="$LIBS" run_timed 30 "$BIN" commands new ../evil >/dev/null 2>&1
  a=$?
  FT_DATA_DIR="$DATA" FT_COMMANDS_DIR="$CMDS" FT_LIBRARY_DIR="$LIBS" run_timed 30 "$BIN" commands new _hidden >/dev/null 2>&1
  b=$?
  FT_DATA_DIR="$DATA" FT_COMMANDS_DIR="$CMDS" FT_LIBRARY_DIR="$LIBS" run_timed 30 "$BIN" library show ../../etc/passwd >/dev/null 2>&1
  c=$?
  if [ "$a" != 0 ] && [ "$b" != 0 ] && [ "$c" != 0 ] && [ ! -e "$WORK/evil.md" ] && [ ! -e "$WORK/etc" ]; then
    CONF_OK=1
  fi
fi
relation F2P-008 "$CONF_OK"

# F2P-009 Go build + run entry point (R038)
if [ "$BUILD_OK" = 1 ] && run_timed 30 "$BIN" --help >/dev/null 2>&1; then relation F2P-009 1; else relation F2P-009 0; fi

# F2P-010 make build artifacts for the documented command surface (R052)
if build_contract_ok; then relation F2P-010 1; else relation F2P-010 0; fi

# F2P-011 production packages with node absent
if dep_policy_ok; then relation F2P-011 1; else relation F2P-011 0; fi

# F2P-012 engine subprocess invocation bridge
if test_ok TestF2P012InvocationBridge; then relation F2P-012 1; else relation F2P-012 0; fi

# F2P-013 filesystem durable write modes
if test_ok TestF2P013FilesystemWriteModes; then relation F2P-013 1; else relation F2P-013 0; fi

# F2P-014 folder/category/domain distributions
if test_ok TestF2P014FoldersAndDistributions; then relation F2P-014 1; else relation F2P-014 0; fi

# F2P-015 engine resolution precedence
if test_ok TestF2P015ResolutionPrecedence; then relation F2P-015 1; else relation F2P-015 0; fi

# F2P-016 schema + content-linked FTS5 + BM25 order
if test_ok TestF2P016SchemaFTSAndBm25Order; then relation F2P-016 1; else relation F2P-016 0; fi

# F2P-017 out-of-scope subcommands fail; in-scope index succeeds
OOO_OK=0
if [ "$BUILD_OK" = 1 ]; then
  f=0
  for c in "sync" "fetch-media" "install app" "app open x"; do
    # shellcheck disable=SC2086
    FT_DATA_DIR="$DATA" run_timed 30 "$BIN" $c >/dev/null 2>&1
    s=$?
    [ "$s" != 0 ] || f=1
  done
  if [ "$f" = 0 ] && FT_DATA_DIR="$DATA" run_timed 60 "$BIN" index >/dev/null 2>&1; then
    OOO_OK=1
  fi
fi
relation F2P-017 "$OOO_OK"

# F2P-018 in-process work with node removed from PATH (R034)
if in_scope_ok; then relation F2P-018 1; else relation F2P-018 0; fi

# F2P-019 search sanitizer + index refresh (R043)
if test_ok TestF2P043IndexRefreshAndSelfHealing && test_ok TestF2P019SearchSanitizerAndEmpty; then
  relation F2P-019 1
else
  relation F2P-019 0
fi

# F2P-020 stats contract + index refresh (R043)
if test_ok TestF2P043IndexRefreshAndSelfHealing && test_ok TestF2P020StatsContract; then
  relation F2P-020 1
else
  relation F2P-020 0
fi

# F2P-021 migrated checks run the real logic (R037)
INTEGRITY_OK=0
if [ "$GOTEST_OK" = 1 ]; then
  CMDBAD="$WORK/commands-bad"; mkdir -p "$CMDBAD"; printf '# Bad\n' >"$CMDBAD/bad.md"
  FT_DATA_DIR="$DATA" FT_COMMANDS_DIR="$CMDBAD" run_timed 30 "$BIN" commands validate >/dev/null 2>&1
  v=$?
  [ "$v" != 0 ] && INTEGRITY_OK=1
fi
relation F2P-021 "$INTEGRITY_OK"

# F2P-022 in-process path surface
if path_surface_ok; then relation F2P-022 1; else relation F2P-022 0; fi

# F2P-023 regex classification over real indexed data + index refresh (R043)
if test_ok TestF2P043IndexRefreshAndSelfHealing; then relation F2P-023 1; else relation F2P-023 0; fi

# F2P-024 --json vs human-readable output
JSON_OK=0
if [ "$BUILD_OK" = 1 ]; then
  FT_DATA_DIR="$DATA" run_timed 30 "$BIN" stats --json >"$WORK/stats.json" 2>/dev/null
  j=$?
  FT_DATA_DIR="$DATA" run_timed 30 "$BIN" stats >"$WORK/stats.txt" 2>/dev/null
  k=$?
  if [ "$j" = 0 ] && [ "$k" = 0 ] && grep -q '"totalBookmarks"' "$WORK/stats.json" && ! grep -q '"totalBookmarks"' "$WORK/stats.txt"; then
    JSON_OK=1
  fi
fi
relation F2P-024 "$JSON_OK"

cat "$FT_RESULTS_FILE"

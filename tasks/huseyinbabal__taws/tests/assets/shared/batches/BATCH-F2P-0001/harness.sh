#!/usr/bin/env bash
# Shared F2P probe harness for BATCH-F2P-0001.
# Probes are described by shared/batches/BATCH-F2P-0001/probes.json.
#
# Usage: harness.sh <repo-root> <report-path>
#
# Every potentially long-running command is wrapped in an explicit `timeout`.
# The harness never waits unbounded and never performs privileged operations.
# The SAME semantic predicate is evaluated for the source (Rust) and the
# reference target (Go): each observation reports the raw measured result, so
# the Rust source fails each Go target-postcondition and the Go target passes.
set -u

REPO="${1:?usage: harness.sh <repo-root> <report-path>}"
REPORT="${2:-/dev/stdout}"

BUILD_TIMEOUT=900
TEST_TIMEOUT=600
CLI_TIMEOUT=30
GIT_TIMEOUT=120

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

RECORDS="$WORKDIR/records.jsonl"
: > "$RECORDS"

record() {
  # record <observation_id> <status> <detail>
  printf '{"observation_id":"%s","status":"%s","detail":"%s"}\n' "$1" "$2" "$3" >> "$RECORDS"
}

emit() {
  # emit <observation_id> <0|1> <detail>
  if [ "$2" -eq 1 ]; then
    record "$1" pass "$3"
  else
    record "$1" fail "$3"
  fi
}

# --- Go target-technology entrypoint -----------------------------------------
build_ok=0
if (cd "$REPO" && timeout "$BUILD_TIMEOUT" go build ./... >/dev/null 2>&1); then
  build_ok=1
fi

BIN="$WORKDIR/taws"
bin_ok=0
if (cd "$REPO" && timeout "$BUILD_TIMEOUT" go build -o "$BIN" . >/dev/null 2>&1); then
  bin_ok=1
elif [ -d "$REPO/cmd/taws" ] && \
     (cd "$REPO" && timeout "$BUILD_TIMEOUT" go build -o "$BIN" ./cmd/taws >/dev/null 2>&1); then
  # A conventional cmd/taws main package is functionally equivalent to a
  # repository-root main package and still produces the required `taws`
  # executable.  Do not make package layout part of the public contract.
  bin_ok=1
fi

module_ok=0
if [ -f "$REPO/go.mod" ]; then
  module_ok=1
fi

# --- CLI identity probes ------------------------------------------------------
version_ok=0
completion_bash_ok=0
completion_elvish_ok=0
loglevel_ok=0
listregions_ok=0
if [ "$bin_ok" -eq 1 ]; then
  v_main="$(timeout "$CLI_TIMEOUT" "$BIN" --version 2>/dev/null)"
  v_short="$(timeout "$CLI_TIMEOUT" "$BIN" -V 2>/dev/null)"
  if [ "$v_main" = "taws 1.3.0-rc.8" ] && [ "$v_short" = "taws 1.3.0-rc.8" ]; then
    version_ok=1
  fi

  # F2P-009 (bash).  The instruction fixes the `completion <shell>` subcommand and
  # its argument-parsing contract, and explicitly relaxes the generated script
  # text ("does not need to match character-for-character").  Assert the observable
  # behaviour of a bash completion script -- it is produced and it registers a
  # completion -- rather than any particular (implementation-defined) function
  # name.  Matching the clap-generated function name wrongly failed
  # functionally-equivalent scripts (e.g. a `_taws_complete()` function plus
  # `complete -F _taws_complete taws`).
  bash_completion="$(timeout "$CLI_TIMEOUT" "$BIN" completion bash 2>/dev/null)"
  if [ -n "$bash_completion" ] \
     && printf '%s\n' "$bash_completion" | grep -Eq '(^|[[:space:]])complete[[:space:]]'; then
    completion_bash_ok=1
  fi

  if [ "$(timeout "$CLI_TIMEOUT" "$BIN" completion elvish 2>/dev/null | wc -l)" -ge 1 ]; then
    completion_elvish_ok=1
  fi

  if [ "$(timeout "$CLI_TIMEOUT" "$BIN" list-regions 2>/dev/null | wc -l)" -ge 1 ]; then
    listregions_ok=1
  fi

  timeout "$CLI_TIMEOUT" "$BIN" --log-level bogus >/dev/null 2>&1
  if [ $? -eq 2 ]; then
    loglevel_ok=1
  fi
fi

completion_ok=$(( completion_bash_ok && completion_elvish_ok ))

# --- Ported public behavioural test suite -------------------------------------
# The frozen source ships public unit tests for the migrated responsibilities
# (configuration, AWS credentials/SigV4/protocols, the resource registry and
# dispatch).  Constraint 8 requires a migration to carry those behavioural
# assertions forward: "Ported or adapted public tests MAY change language, but
# their behavioural assertions MUST be preserved".  The contract fixes
# observable behaviour, not the Go package layout or the reference's exact Go
# test names, so the ported suite may live in any package and use any test
# names.  A tree that carries no behavioural tests at all preserved nothing and
# cannot demonstrate the migrated postconditions.
ported_suite_ok=0
if [ "$module_ok" -eq 1 ]; then
  if (cd "$REPO" && timeout "$TEST_TIMEOUT" go test -count=1 ./... >/dev/null 2>&1); then
    if (cd "$REPO" && timeout "$GIT_TIMEOUT" grep -RIl --include='*_test.go' --exclude-dir=vendor 'func Test' . >/dev/null 2>&1); then
      ported_suite_ok=1
    fi
  fi
fi

# --- Dependency / process boundary probes -------------------------------------
deps_ok=0
if (cd "$REPO" && timeout "$TEST_TIMEOUT" go list -deps ./... >"$WORKDIR/deps.txt" 2>/dev/null); then
  if ! grep -qE 'cargo|\.rs$' "$WORKDIR/deps.txt"; then
    deps_ok=1
  fi
fi

# The migration contract fixes observable behaviour, not the Go package layout,
# so these tree scans cover the whole module (the migrated code may live at the
# root, under a command directory, or under internal/).  Vendor trees are
# excluded so third-party sources cannot mask a genuine reference.
no_tui_refs=0
if [ "$module_ok" -eq 1 ]; then
  # Test provenance and comments cannot bind a production Go binary to Rust.
  # Preserve the reference check on production code and string literals.
  if timeout "$GIT_TIMEOUT" python3 - "$REPO" <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
tokens = re.compile(r'("(?:\\.|[^"\\])*"|`[^`]*`|\'(?:\\.|[^\'\\])*\')|//[^\n]*|/\*[\s\S]*?\*/')
for path in root.rglob('*.go'):
    relative = path.relative_to(root)
    if path.is_symlink() or path.name.endswith('_test.go') or any(
        part in {'.git', 'vendor', 'testdata'} for part in relative.parts
    ):
        continue
    source = path.read_text(encoding='utf-8', errors='replace')
    production = tokens.sub(lambda match: match.group(1) or '', source)
    if any(marker in production for marker in ('src/ui', 'src/app', 'app.rs', 'event.rs')):
        sys.exit(1)
PY
  then
    no_tui_refs=1
  fi
fi

exec_ok=0
if [ "$module_ok" -eq 1 ] && \
   (cd "$REPO" && timeout "$GIT_TIMEOUT" grep -RIl --include=*.go --exclude-dir=vendor 'exec.Command' . >/dev/null 2>&1); then
  if ! (cd "$REPO" && timeout "$GIT_TIMEOUT" grep -RIn --include=*.go --exclude-dir=vendor \
      -e 'cargo' -e 'rustc' . >/dev/null 2>&1); then
    exec_ok=1
  fi
fi

ssm_ok=0
if [ "$module_ok" -eq 1 ] && \
   (cd "$REPO" && timeout "$GIT_TIMEOUT" grep -RIq --include='*.go' 'SSM session exited with code' .); then
  ssm_ok=1
fi

# --- Migration working-tree probes --------------------------------------------
hidden_ok=0
if [ "$module_ok" -eq 1 ]; then
  if (cd "$REPO" && timeout "$GIT_TIMEOUT" git rev-parse --is-inside-work-tree >/dev/null 2>&1); then
    if ! (cd "$REPO" && timeout "$GIT_TIMEOUT" git status --porcelain 2>/dev/null | grep -Eq '(^|/)(tests|verifier)/|verifier'); then
      hidden_ok=1
    fi
  else
    hidden_ok=1
  fi
fi

worktree_ok=0
if [ "$module_ok" -eq 1 ] && \
   (cd "$REPO" && timeout "$GIT_TIMEOUT" git rev-parse --is-inside-work-tree >/dev/null 2>&1); then
  if (cd "$REPO" && timeout "$GIT_TIMEOUT" git status --porcelain 2>/dev/null | grep -q .); then
    worktree_ok=1
  fi
fi

# --- Catalog immutability and embedding ---------------------------------------
catalog_ok=1
if (cd "$REPO" && timeout "$GIT_TIMEOUT" git cat-file -e \
    d1cbf6e450eced9a7947cf810764ec79599c7793^{commit} >/dev/null 2>&1); then
  (cd "$REPO" && timeout "$GIT_TIMEOUT" git diff --exit-code \
    d1cbf6e450eced9a7947cf810764ec79599c7793 -- src/resources) >/dev/null 2>&1 || catalog_ok=0
elif [ -d /input/source/src/resources ] && [ -d "$REPO/src/resources" ]; then
  timeout "$GIT_TIMEOUT" diff -r /input/source/src/resources "$REPO/src/resources" >/dev/null 2>&1 || catalog_ok=0
elif [ ! -d "$REPO/src/resources" ]; then
  catalog_ok=0
fi

embed_ok=0
if (cd "$REPO" && timeout "$GIT_TIMEOUT" grep -RIl --include=*.go --exclude-dir=vendor 'go:embed' . >/dev/null 2>&1); then
  embed_ok=1
fi

# --- Non-interactive fetch/act network probes (F2P-025..F2P-031) --------------
# These drive the built binary against a loopback mock AWS endpoint and assert
# the declared protocol/SigV4/pagination/read-only/endpoint contract.  The
# probes are implemented in Python (JSON parsing + a local HTTP server); a
# missing binary or interpreter is reported as FAIL, never silently passed.
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
NET_PROBES="$HERE/network_probes.py"
NET_TIMEOUT=300
if [ "$bin_ok" -eq 1 ] && command -v python3 >/dev/null 2>&1 && [ -f "$NET_PROBES" ]; then
  timeout "$NET_TIMEOUT" python3 "$NET_PROBES" --bin "$BIN" --records "$RECORDS" >/dev/null 2>&1 || true
else
  for oid in F2P-005 F2P-009 F2P-012 F2P-025 F2P-026 F2P-027 F2P-028 F2P-029 F2P-030 F2P-031; do
    record "$oid" fail 'non-interactive network probes unavailable (binary or python3 missing)'
  done
fi

# --- Emit one record per owned observation, in inventory order ----------------
emit F2P-001 "$(( build_ok && version_ok ))"   'go build and version entrypoint'
emit F2P-002 "$ported_suite_ok"                'ported behavioural suite: AWS request construction'
emit F2P-003 "$hidden_ok"                      'no hidden evaluation asset changed'
emit F2P-004 "$worktree_ok"                    'migration present as working-tree changes'
emit F2P-005 "$ported_suite_ok"                'ported behavioural suite: endpoint override resolution'
emit F2P-006 "$deps_ok"                        'Go dependency graph stays in-process'
emit F2P-008 "$no_tui_refs"                    'no Go package binds the Rust TUI'
emit F2P-009 "$(( bin_ok && completion_ok && listregions_ok ))" 'completion and list subcommands'
emit F2P-010 "$ported_suite_ok"                'ported behavioural suite: read-only guard'
emit F2P-011 "$ported_suite_ok"                'ported behavioural suite: custom CA bundle'
emit F2P-013 "$ssm_ok"                         'SSM session exit code reporting'
emit F2P-014 "$loglevel_ok"                    'log-level validation rejects bad value'
emit F2P-015 "$(( catalog_ok && ported_suite_ok ))" 'catalog unchanged and resource-key behaviour tested'
emit F2P-016 "$exec_ok"                        'external command boundary retained'
emit F2P-018 "$(( catalog_ok && embed_ok ))"    'catalog embedded and unchanged'
emit F2P-021 "$ported_suite_ok"                'ported behavioural suite: registry schema decoding'
emit F2P-024 "$ported_suite_ok"                'ported behavioural suite: JSON protocol headers'
emit F2P-012 "$ported_suite_ok"                'ported behavioural suite: credential chain'
emit F2P-007 "$ported_suite_ok"                'ported behavioural suite: AWS request construction'
emit F2P-017 "$(( bin_ok && version_ok ))"      'version flag output'
emit F2P-019 "$(( catalog_ok && ported_suite_ok ))" 'requires_parent filtering behaviour tested'
emit F2P-020 "$no_tui_refs"                     'migrated responsibilities are Go-only'
emit F2P-022 "$ported_suite_ok"                'ported behavioural suite: endpoint override precedence'
emit F2P-023 "$(( catalog_ok && ported_suite_ok ))" 'resource keys/required fields behaviour tested'

# Multiple predicates for one observation are conjunctive: retain the existing
# ported-suite checks as well as the independent CLI checks, then emit one row.
python3 - "$RECORDS" "$REPORT" <<'PY'
import json
import sys

merged = {}
with open(sys.argv[1], encoding="utf-8") as handle:
    for line in handle:
        row = json.loads(line)
        key = row["observation_id"]
        if key in merged:
            previous = merged[key]
            row["status"] = "pass" if previous["status"] == row["status"] == "pass" else "fail"
            row["detail"] = previous["detail"] + "; " + row["detail"]
        merged[key] = row
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(list(merged.values()), handle, indent=2)
    handle.write("\n")
PY

cat "$REPORT"

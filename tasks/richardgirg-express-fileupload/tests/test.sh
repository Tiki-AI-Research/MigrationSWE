#!/bin/bash
# Deterministic migration verifier entrypoint (reward_mode = VERIFIER_ONLY).
#
# Pipeline:  hard gate -> completion verifier -> gate aggregation -> reports
#
# Every resource is resolved relative to this script or to container-absolute
# locations: the tests live in /tests, the candidate repository in
# /workspace/repo, the candidate patch in /logs/artifacts/model.patch and all
# reports in /logs/verifier. No host-specific path is ever referenced.
set -Eeuo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGS="${MIGRATION_LOGS_DIR:-/logs/verifier}"
ARTIFACTS="${MIGRATION_ARTIFACTS_DIR:-/logs/artifacts}"
REPO="${SWEBENCH_REPO_PATH:-${ENVPIPE_REPO_PATH:-/workspace/repo}}"
MODEL_PATCH="${MIGRATION_MODEL_PATCH:-${ARTIFACTS}/model.patch}"
CONFIG="${TESTS_DIR}/config.json"
VERIFIER_DIR="${TESTS_DIR}/verifier"
ASSETS_DIR="${TESTS_DIR}/assets"

REPORTS="${LOGS}/reports"
NODE_REPORT="${REPORTS}/nodes.json"
HARD_GATE_REPORT="${LOGS}/hard-gate.json"
VERIFIER_REPORT="${LOGS}/verifier-result.json"
GATE_REWARD="${LOGS}/gate-reward.json"
CTRF_REPORT="${LOGS}/ctrf.json"
REWARD="${LOGS}/reward.json"
REWARD_DETAILS="${LOGS}/reward-details.json"
REWARD_TXT="${LOGS}/reward.txt"

PYTHON="${MIGRATION_PYTHON:-$(command -v python3 2>/dev/null || echo python3)}"
# Recorded in reward-details.json so a privilege change is always auditable.
export MIGRATION_EFFECTIVE_USER="${MIGRATION_EFFECTIVE_USER:-$(id -un 2>/dev/null || id -u)}"

# Resolve the node interpreter without swallowing failures: a missing node must
# surface as an infrastructure error instead of being masked by ``|| true``.
NODE_BIN="${MIGRATION_NODE:-}"
if [ -z "$NODE_BIN" ]; then
  if ! NODE_BIN="$(command -v node 2>/dev/null)"; then
    NODE_BIN=""
  fi
fi

mkdir -p "$LOGS" "$REPORTS"
rm -f "$CTRF_REPORT" "$REWARD" "$REWARD_DETAILS" "$REWARD_TXT" \
      "$NODE_REPORT" "$HARD_GATE_REPORT" "$VERIFIER_REPORT" "$GATE_REWARD"
rm -rf -- "${REPORTS}/hardgate"

# Infrastructure failures (dependency, git, evaluator crash) must never leave a
# valid reward.json behind: the harness reads reward.txt = -1 instead.
infrastructure_failure() {
  local status=$?
  if [ -n "${1-}" ]; then status="$1"; fi
  trap - ERR
  rm -f "$CTRF_REPORT" "$REWARD" "$REWARD_DETAILS"
  printf '%s\n' -1 > "$REWARD_TXT"
  exit "$status"
}
trap infrastructure_failure ERR

# The hidden F2P payload is never executed from the candidate's own worktree:
# that tree is owned by the agent, so a node running in-process could delete or
# rewrite the nodes that have not run yet and have the forged exit codes scored
# (QC ADV-F2P-MUTABLE-PAYLOAD). It is applied once, then moved into a root-owned
# directory under /tests -- the candidate identity can read it but never write
# it -- and the verifier re-hashes it before and after execution.
F2P_STAGE="${TESTS_DIR}/.f2p_runtime"
F2P_PAYLOAD_STAGE="${F2P_STAGE}/test/_koa_migration_cases.js"

cleanup_f2p_payload() {
  rm -f "$REPO/test/_koa_migration_cases.js"
  rm -rf "$F2P_STAGE"
}
trap cleanup_f2p_payload EXIT

stage_f2p_payload() {
  git -C "$REPO" apply --whitespace=nowarn --binary "$VERIFIER_DIR/test.patch"
  rm -rf "$F2P_STAGE"
  mkdir -p "$F2P_STAGE/test"
  mv "$REPO/test/_koa_migration_cases.js" "$F2P_PAYLOAD_STAGE"
  # ``require('../lib/index')`` from the staged payload must resolve to the
  # candidate implementation; the symlink is root-owned like the directory that
  # contains it, so the candidate cannot repoint it.
  ln -sfn "$REPO/lib" "$F2P_STAGE/lib"
  chmod 0755 "$F2P_STAGE" "$F2P_STAGE/test"
  chmod 0644 "$F2P_PAYLOAD_STAGE"
}

# --- candidate sandbox: namespaces + privilege drop ------------------------
# The verifier runs as root so it can read the frozen evaluation assets, but
# every subprocess that executes candidate code is wrapped by
# verifier/sandbox.py in `unshare --net --pid --fork --mount-proc --kill-child`
# and then dropped to the unprivileged identity below with an empty capability
# set (`setpriv ... --bounding-set=-all --no-new-privs`). The namespace barrier
# needs CAP_SYS_ADMIN + CAP_NET_ADMIN and an unconfined AppArmor profile, which
# tests/docker-compose.yaml grants -- and nothing more.
#
# There is deliberately no silent fallback: if the sandbox cannot be built, the
# run is an infrastructure failure (reward.txt = -1) instead of an unisolated
# evaluation that would hand candidate code both root and the network.
SANDBOX_USER="${MIGRATION_SANDBOX_USER-agent}"
if [ "$(id -u 2>/dev/null || echo 1)" -eq 0 ] && [ -n "$SANDBOX_USER" ] \
    && ! id -u "$SANDBOX_USER" >/dev/null 2>&1; then
  if ! useradd --system --create-home --shell /usr/sbin/nologin "$SANDBOX_USER" >/dev/null 2>&1; then
    echo "FATAL: sandbox user '$SANDBOX_USER' is unavailable; refusing to run candidate code as root" >&2
    infrastructure_failure 1
  fi
fi
export MIGRATION_SANDBOX_USER="$SANDBOX_USER"
for sandbox_tool in unshare setpriv; do
  if ! command -v "$sandbox_tool" >/dev/null 2>&1; then
    echo "FATAL: '$sandbox_tool' is missing; the candidate sandbox cannot be built" >&2
    infrastructure_failure 1
  fi
done

# --- 0. Lightweight gate self-check, before any reward is written.
# Re-runs the skip-only and assertion-weakening canaries against this hard gate.
# A mismatch (skip-only scored as cheating, or a weakened assertion let through)
# is an infrastructure error: reward.txt = -1 and no scored reward.json.
CANARY_JSON="${LOGS}/canary-startup.json"
canary_rc=0
"$PYTHON" "$VERIFIER_DIR/canary_suite.py" \
  --repo "$REPO" \
  --tests-dir "$TESTS_DIR" \
  --startup-selfcheck \
  --json-output "$CANARY_JSON" || canary_rc=$?
if [ "$canary_rc" -ne 0 ]; then
  echo "FATAL: canary startup self-check failed (rc=${canary_rc}); refusing to score this run" >&2
  infrastructure_failure 2
fi

# --- 1. Hard gate: real atoms, run against a clean base checkout + candidate patch.
hg_rc=0
hardgate_args=( "$PYTHON" "$VERIFIER_DIR/hardgate.py"
  --config "$CONFIG"
  --repo "$REPO"
  --patch-file "$MODEL_PATCH"
  --output "$HARD_GATE_REPORT"
  --logs-dir "$LOGS"
  --tests-dir "$TESTS_DIR" )
if [ -n "$NODE_BIN" ]; then hardgate_args+=( --node "$NODE_BIN" ); fi
"${hardgate_args[@]}" || hg_rc=$?

# An unreadable/unbuildable sandbox, a dirty evaluation tree or a crashed gate
# invalidates the evaluation: the harness must see reward.txt = -1 rather than a
# scored run whose verdict was produced without the isolation it claims.
hg_infra=$("$PYTHON" - "$HARD_GATE_REPORT" <<'PY'
import json, sys
try:
    doc = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("1")
    raise SystemExit(0)
print("1" if doc.get("infrastructure_error") else "0")
PY
)
if [ "$hg_infra" = "1" ]; then
  echo "FATAL: hard gate reported an infrastructure error; refusing to score this run" >&2
  infrastructure_failure 2
fi

hg_pass=$("$PYTHON" - "$HARD_GATE_REPORT" <<'PY'
import json, sys
try:
    doc = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("0")
    raise SystemExit(0)
ok = (
    doc.get("success") is True
    and doc.get("status") == "pass"
    and not doc.get("infrastructure_error")
)
print("1" if ok else "0")
PY
)

# --- 2. Apply the candidate patch only when the gate accepted it.
apply_failed=1
if [ "$hg_pass" = "1" ]; then
  apply_failed=0
  if [ -s "$MODEL_PATCH" ]; then
    if git -C "$REPO" apply --check --whitespace=nowarn --binary "$MODEL_PATCH" >/dev/null 2>&1; then
      git -C "$REPO" apply --whitespace=nowarn --binary "$MODEL_PATCH"
    elif ! git -C "$REPO" apply --check -R --whitespace=nowarn --binary "$MODEL_PATCH" >/dev/null 2>&1; then
      apply_failed=1
    fi
  fi
fi

# The hard gate grades ``base + model.patch`` and drops every untracked
# workspace file the patch does not declare (patch closure, QC
# RC-PATCH-CLOSURE): ``git diff <base> HEAD`` never contains them, so they must
# not influence the behavioural suite either. Mirror the gate's drop on the
# graded worktree so both run against exactly the same tree.
if [ "$hg_pass" = "1" ]; then
  "$PYTHON" - "$HARD_GATE_REPORT" "$REPO" <<'PY'
import json
import sys
from pathlib import Path

report, repo = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
try:
    doc = json.loads(report.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
for rel in doc.get("untracked_paths_dropped") or []:
    target = repo / str(rel)
    try:
        if target.is_file() or target.is_symlink():
            target.unlink()
    except OSError:
        pass
PY
fi

if [ "$hg_pass" = "1" ] && [ "$apply_failed" -eq 0 ]; then
  stage_f2p_payload
fi

# --- 3. Run the verifier.
verifier_args=( "$PYTHON" "$VERIFIER_DIR/verifier.py"
  --config "$CONFIG"
  --repo "$REPO"
  --ctrf-output "$CTRF_REPORT"
  --node-report "$NODE_REPORT"
  --migration-units "$ASSETS_DIR/migration_units.json"
  --f2p-manifest "$ASSETS_DIR/f2p_manifest.json"
  --p2p-manifest "$ASSETS_DIR/p2p_manifest.json"
  --verifier-result "$VERIFIER_REPORT"
  --hard-gate-report "$HARD_GATE_REPORT"
  --tests-dir "$TESTS_DIR"
  --apply-failed "$apply_failed" )
if [ "$hg_pass" = "1" ] && [ "$apply_failed" -eq 0 ]; then
  verifier_args+=( --f2p-payload "$F2P_PAYLOAD_STAGE" )
fi
if [ -n "$NODE_BIN" ]; then verifier_args+=( --node "$NODE_BIN" ); fi

verifier_rc=0
"${verifier_args[@]}" || verifier_rc=$?

cleanup_f2p_payload

if [ "$verifier_rc" -ne 0 ]; then
  infrastructure_failure "$verifier_rc"
fi

# --- 4. Aggregate gate + verifier evidence.
gate_rc=0
"$PYTHON" "${TESTS_DIR}/grader.py" gate \
  --config "$CONFIG" \
  --ctrf "$CTRF_REPORT" \
  --hard-gate "$HARD_GATE_REPORT" \
  --verifier-result "$VERIFIER_REPORT" \
  --node-report "$NODE_REPORT" \
  --output "$GATE_REWARD" || gate_rc=$?
if [ "$gate_rc" -ne 0 ]; then
  infrastructure_failure "$gate_rc"
fi

# --- 5. Final reports.
final_rc=0
"$PYTHON" "${TESTS_DIR}/grader.py" finalize \
  --config "$CONFIG" \
  --gate "$GATE_REWARD" \
  --hard-gate "$HARD_GATE_REPORT" \
  --output-reward "$REWARD" \
  --output-details "$REWARD_DETAILS" \
  --model-patch "$MODEL_PATCH" || final_rc=$?

if [ "$final_rc" -ne 0 ]; then
  infrastructure_failure "$final_rc"
fi

test -f "$REWARD"
test -f "$REWARD_DETAILS"
test -f "$CTRF_REPORT"
test -f "$VERIFIER_REPORT"
test -f "$NODE_REPORT"
test ! -e "$REWARD_TXT"
trap - ERR
exit 0

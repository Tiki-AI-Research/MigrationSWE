#!/bin/sh
set -eu

LOG="/logs/verifier"
REPORTS="$LOG/reports"
REPO="/workspace/repo"
MODEL_PATCH="/logs/artifacts/model.patch"
APPLY_LOG="$LOG/patch-apply.txt"
HARD_GATE_REPORT="$LOG/hard-gate.json"
VERIFIER_REPORT="$LOG/verifier-result.json"
REWARD_JSON="$LOG/reward.json"
ASSET_INTEGRITY_REPORT="$LOG/asset-integrity.json"

# 1. Clean prior reports and init dirs
mkdir -p "$REPORTS" /logs/artifacts
rm -f "$LOG"/*.json
rm -f "$REPORTS"/*.json
exec > "$LOG/test.stdout" 2> "$LOG/test.stderr"

record_runner() {
  python3 - "$@" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    report = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError):
    report = {"schema_version": "harbor-migration-check/v1", "success": False, "status": "infrastructure_error"}
report["runner"] = {"exit_code": int(sys.argv[2]), "timed_out": False, "stage": sys.argv[3], "stdout_path": sys.argv[4], "stderr_path": sys.argv[5], "command": sys.argv[6:]}
path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
PY
}

# Trap unexpected script errors and always produce all terminal reports.
on_err() {
  status=$?
  trap - EXIT
  if [ ! -f "$REWARD_JSON" ] || [ ! -f "$LOG/reward-details.json" ] || [ ! -f "$LOG/ctrf.json" ]; then
    HARBOR_LOG_DIR="/logs" HARBOR_CONFIG_PATH="/tests/config.json" \
      HARBOR_FAILED_STAGE="${CURRENT_STAGE:-test_orchestration}" HARBOR_FAILURE_EXIT_CODE="$status" \
      python3 -B /tests/grader.py --reason unexpected_error >/dev/null 2>&1 || true
  fi
  if [ "$status" -eq 0 ]; then
    status=1
  fi
  exit "$status"
}
trap on_err EXIT
CURRENT_STAGE="asset_integrity"

# Evaluator-owned assets must be validated before any contract, baseline, or
# evaluation patch is consumed.
asset_rc=0
python3 -B /tests/verifier/asset_integrity.py --assets /tests/assets --out "$ASSET_INTEGRITY_REPORT" \
  > "$LOG/asset-integrity.stdout" 2> "$LOG/asset-integrity.stderr" || asset_rc=$?
if [ "$asset_rc" -ne 0 ]; then
  printf '%s\n' '{"schema_version":"harbor-migration-check/v1","id":"hard_gate","hard_gate_passed":false,"status":"configuration_error","summary":"evaluator asset manifest validation failed","gates":{},"evidence":{"changed_files":[],"policy_reasons":[]}}' > "$HARD_GATE_REPORT"
  record_runner "$HARD_GATE_REPORT" "$asset_rc" asset_integrity "$LOG/asset-integrity.stdout" "$LOG/asset-integrity.stderr" \
    python3 -B /tests/verifier/asset_integrity.py --assets /tests/assets --out "$ASSET_INTEGRITY_REPORT"
  grader_rc=0
  python3 -B /tests/grader.py --reason asset_integrity_failed || grader_rc=$?
  trap - EXIT
  if [ "$grader_rc" -ne 0 ]; then
    exit "$grader_rc"
  fi
  exit 2
fi

# Recheck both the detector and its hard-gate-to-grader wiring before reading
# or executing any candidate patch.
CURRENT_STAGE="security_selftest"
: > "$LOG/security-selftest.stdout"
: > "$LOG/security-selftest.stderr"
for security_check in legacy_require_selftest hardgate_priority_selftest; do
  security_rc=0
  python3 -B "/tests/verifier/$security_check.py" \
    >> "$LOG/security-selftest.stdout" 2>> "$LOG/security-selftest.stderr" || security_rc=$?
  if [ "$security_rc" -ne 0 ]; then
    printf '%s\n' '{"schema_version":"harbor-migration-check/v1","id":"hard_gate","hard_gate_passed":false,"status":"infrastructure_error","summary":"security self-test failed","gates":{},"evidence":{"changed_files":[],"policy_reasons":[]}}' > "$HARD_GATE_REPORT"
    record_runner "$HARD_GATE_REPORT" "$security_rc" security_selftest "$LOG/security-selftest.stdout" "$LOG/security-selftest.stderr" \
      python3 -B "/tests/verifier/$security_check.py"
    grader_rc=0
    python3 -B /tests/grader.py --reason security_selftest_failed || grader_rc=$?
    trap - EXIT
    if [ "$grader_rc" -ne 0 ]; then
      exit "$grader_rc"
    fi
    exit 2
  fi
done

# A transport change alters the hidden-patch hash. Re-measure base and gold
# before candidate execution instead of relabeling historical evidence with
# the new hash. Matching evidence is kept without running the suites again.
CURRENT_STAGE="baseline_refresh"
baseline_rc=0
python3 -B /tests/verifier/refresh_baseline.py --if-stale \
  > "$LOG/baseline-refresh.stdout" 2> "$LOG/baseline-refresh.stderr" || baseline_rc=$?
if [ "$baseline_rc" -ne 0 ]; then
  printf '%s\n' '{"schema_version":"harbor-migration-check/v1","id":"hard_gate","hard_gate_passed":false,"status":"infrastructure_error","summary":"baseline refresh failed before candidate execution","gates":{},"evidence":{"changed_files":[],"policy_reasons":[]}}' > "$HARD_GATE_REPORT"
  record_runner "$HARD_GATE_REPORT" "$baseline_rc" baseline_refresh "$LOG/baseline-refresh.stdout" "$LOG/baseline-refresh.stderr" \
    python3 -B /tests/verifier/refresh_baseline.py --if-stale
  grader_rc=0
  python3 -B /tests/grader.py --reason baseline_refresh_failed || grader_rc=$?
  trap - EXIT
  if [ "$grader_rc" -ne 0 ]; then
    exit "$grader_rc"
  fi
  exit 2
fi

# 2. The separate verifier receives the candidate patch through Harbor's
# artifact transfer. Patch collection itself runs in the agent environment via
# verifier.collect; re-exporting here would inspect the clean verifier baseline
# and overwrite the transferred candidate patch with an empty diff.
if [ -f /opt/sharing-base/.base_commit ]; then
  cp /opt/sharing-base/.base_commit "$REPO/.base_commit"
fi

# Keep the fixed patch-export stage observable for the standalone verifier.
# Harbor already transfers the candidate artifact to MODEL_PATCH; exporting
# into a diagnostic directory avoids replacing that artifact with a diff of
# the clean verifier checkout.
PATCH_EXPORT_DIAGNOSTIC_DIR="$LOG/patch-export-diagnostic"
mkdir -p "$PATCH_EXPORT_DIAGNOSTIC_DIR"
patch_export_rc=0
CURRENT_STAGE="patch_export"
python3 /tests/patch_export.py --repo "$REPO" --logs "$PATCH_EXPORT_DIAGNOSTIC_DIR" \
  > "$LOG/patch-export.stdout" 2> "$LOG/patch-export.stderr" || patch_export_rc=$?
if [ "$patch_export_rc" -ne 0 ]; then
  printf '%s\n' "patch export diagnostic exited with code $patch_export_rc" > "$LOG/patch-export-status.txt"
fi

# 3. Apply /logs/artifacts/model.patch with git apply --binary (record apply_failed)
apply_failed=0
apply_rc=0
CURRENT_STAGE="patch_apply"
: > "$LOG/patch-apply.stderr"
if [ ! -d "$REPO" ] || [ ! -f "$MODEL_PATCH" ]; then
  printf '%s\n' 'candidate repository or model patch is missing, or patch export failed' > "$APPLY_LOG"
  apply_failed=1
  apply_rc=1
elif [ -s "$MODEL_PATCH" ]; then
  if git -C "$REPO" apply --check --binary "$MODEL_PATCH" > "$APPLY_LOG" 2> "$LOG/patch-apply.stderr"; then
    git -C "$REPO" apply --binary "$MODEL_PATCH" >> "$APPLY_LOG" 2>> "$LOG/patch-apply.stderr" || { apply_rc=$?; apply_failed=1; }
  else
    apply_rc=$?
    apply_failed=1
  fi
else
  printf '%s\n' 'candidate patch is empty' > "$APPLY_LOG"
fi

# Candidate production code is executed only from evaluator-created writable
# copies. The patched canonical repository remains root-only so candidate code
# cannot inspect Git metadata or mutate the evaluator's source of truth.
if [ -d "$REPO" ]; then
  chmod -R go-rwx "$REPO"
fi

# A missing or unappliable candidate patch is a candidate failure. Do not run
# the remaining gates against the untouched verifier baseline.
if [ "$apply_failed" -ne 0 ]; then
  printf '%s\n' '{"schema_version":"harbor-migration-check/v1","id":"hard_gate","hard_gate_passed":false,"status":"candidate_failure","summary":"candidate patch is missing or cannot be applied","gates":{"candidate_valid":{"id":"candidate_valid","success":false,"status":"candidate_failure","summary":"candidate patch is missing or cannot be applied"}},"evidence":{"changed_files":[],"policy_reasons":[],"apply_log":"'"$APPLY_LOG"'"}}' > "$HARD_GATE_REPORT"
  record_runner "$HARD_GATE_REPORT" "$apply_rc" patch_apply "$APPLY_LOG" "$LOG/patch-apply.stderr" \
    git -C "$REPO" apply --check --binary "$MODEL_PATCH"
  grader_rc=0
  python3 /tests/grader.py --reason patch_apply_failed || grader_rc=$?
  trap - EXIT
  if [ "$grader_rc" -ne 0 ]; then
    exit "$grader_rc"
  fi
  exit 1
fi

# 4. Run hardgate.py -> /logs/verifier/hard-gate.json
# If not passed -> grader.py -> non-zero exit
gate_rc=0
CURRENT_STAGE="hard_gate"
python3 /tests/verifier/hardgate.py --repo "$REPO" --patch "$MODEL_PATCH" --config /tests/config.json --out "$HARD_GATE_REPORT" \
  > "$LOG/hardgate.stdout" 2> "$LOG/hardgate.stderr" || gate_rc=$?
record_runner "$HARD_GATE_REPORT" "$gate_rc" hard_gate "$LOG/hardgate.stdout" "$LOG/hardgate.stderr" \
  python3 /tests/verifier/hardgate.py --repo "$REPO" --patch "$MODEL_PATCH" --config /tests/config.json --out "$HARD_GATE_REPORT"

gate_passed=0
if [ "$apply_failed" -eq 0 ] && [ "$gate_rc" -eq 0 ] && [ -f "$HARD_GATE_REPORT" ]; then
  if python3 -c 'import json, sys; res = json.load(open(sys.argv[1])); sys.exit(0 if res.get("hard_gate_passed") is True else 1)' "$HARD_GATE_REPORT" 2>/dev/null; then
    gate_passed=1
  fi
fi

if [ "$gate_passed" -ne 1 ]; then
  grader_rc=0
  python3 /tests/grader.py --reason hard_gate_failed || grader_rc=$?
  trap - EXIT
  if [ "$grader_rc" -ne 0 ]; then
    exit "$grader_rc"
  fi
  gate_status="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status", "candidate_failure"))' "$HARD_GATE_REPORT" 2>/dev/null || printf '%s' 'infrastructure_error')"
  case "$gate_status" in
    infrastructure_error|configuration_error) exit 2 ;;
    *) exit 1 ;;
  esac
fi

# 5. Run verifier.py -> /logs/verifier/verifier-result.json
# If not success -> grader.py -> non-zero exit
verifier_rc=0
CURRENT_STAGE="verifier"
python3 /tests/verifier/verifier.py --config /tests/config.json --repo "$REPO" --out "$VERIFIER_REPORT" \
  > "$LOG/verifier.stdout" 2> "$LOG/verifier.stderr" || verifier_rc=$?
record_runner "$VERIFIER_REPORT" "$verifier_rc" verifier "$LOG/verifier.stdout" "$LOG/verifier.stderr" \
  python3 /tests/verifier/verifier.py --config /tests/config.json --repo "$REPO" --out "$VERIFIER_REPORT"

verifier_passed=0
if [ "$verifier_rc" -eq 0 ] && [ -f "$VERIFIER_REPORT" ]; then
  if python3 -c 'import json, sys; res = json.load(open(sys.argv[1])); sys.exit(0 if res.get("success") is True else 1)' "$VERIFIER_REPORT" 2>/dev/null; then
    verifier_passed=1
  fi
fi

if [ "$verifier_passed" -ne 1 ]; then
  grader_rc=0
  python3 /tests/grader.py --reason verifier_failed || grader_rc=$?
  trap - EXIT
  if [ "$grader_rc" -ne 0 ]; then
    exit "$grader_rc"
  fi
  verifier_status="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status", "candidate_failure"))' "$VERIFIER_REPORT" 2>/dev/null || printf '%s' 'infrastructure_error')"
  case "$verifier_status" in
    infrastructure_error|configuration_error) exit 2 ;;
    *) exit 1 ;;
  esac
fi

# 6. Run grader.py --reason scored and reflect the resulting state in exit code
grader_rc=0
CURRENT_STAGE="finalize"
python3 /tests/grader.py --reason scored || grader_rc=$?

if [ "$grader_rc" -ne 0 ]; then
  trap - EXIT
  exit "$grader_rc"
fi

# The grader's reward-details.json status is the single source of truth for the
# final exit code: a measured run succeeds, evaluator failures exit 2, and any
# other terminal status is a candidate failure.
final_status="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status", "infrastructure_error"))' "$LOG/reward-details.json" 2>/dev/null || printf '%s' 'infrastructure_error')"

# Clear trap before normal exit
trap - EXIT
case "$final_status" in
  measured) exit 0 ;;
  infrastructure_error|configuration_error|not_run|not_measured) exit 2 ;;
  *) exit 1 ;;
esac

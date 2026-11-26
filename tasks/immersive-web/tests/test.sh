#!/bin/bash
set -Eeuo pipefail

LOGS=/logs/verifier
REPORTS="$LOGS/reports"
MODEL_PATCH=/logs/artifacts/model.patch
MODEL_MANIFEST=/logs/artifacts/model.patch.manifest.json
REPO=/workspace/repo
CONFIG=/tests/config.json
CTRF_REPORT="$LOGS/ctrf.json"
HARD_GATE_REPORT="$LOGS/hard-gate.json"
GATE_REWARD="$LOGS/gate-reward.json"
VERIFIER_RESULT="$LOGS/verifier-result.json"
REWARD_DETAILS="$LOGS/reward-details.json"
REWARD="$LOGS/reward.json"
REWARD_TEXT="$LOGS/reward.txt"

mkdir -p "$REPORTS"
# Hidden evaluation outputs stay out of reach of the candidate identity on
# runtimes that enforce these modes. /logs/verifier is a platform bind-mount
# root, so this is best-effort; hardgate.py separately records whether the
# runtime actually enforced it and fails only the evaluator-owned /tests paths.
chmod 0700 "$LOGS" 2>/dev/null || true
chmod 0750 "$REPORTS" 2>/dev/null || true
rm -f "$CTRF_REPORT" "$REWARD" "$REWARD_DETAILS" "$REWARD_TEXT" \
  "$HARD_GATE_REPORT" "$GATE_REWARD" "$LOGS/infrastructure-error.json" \
  "$LOGS/build-evidence.json" "$LOGS/verifier-result.json"

write_infrastructure_error() {
  phase=$1
  message=$2
  python3 - "$LOGS/infrastructure-error.json" "$phase" "$message" <<'PY'
import json
import sys
import time
from pathlib import Path

path, phase, message = sys.argv[1:]
payload = {
    "schema_version": "evaluation-infrastructure-error/v1",
    "error_class": "evaluation_environment_or_verifier_error",
    "responsibility": "evaluator_or_platform",
    "evaluation_valid": False,
    "retryable": True,
    "phase": phase,
    "message": message,
    "timestamp_ms": int(time.time() * 1000),
}
try:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
except OSError:
    pass
PY
}

infrastructure_failure() {
  status=$?
  trap - ERR
  # Preserve a more specific infrastructure diagnosis (e.g. the verifier's
  # network/asset isolation failure) instead of overwriting it.
  if [ ! -s "$LOGS/infrastructure-error.json" ]; then
    write_infrastructure_error "test_or_grader" "test harness command failed with exit $status"
  fi
  # Infrastructure failures are not candidate cheating: never emit the
  # anti-cheating sentinel (-1). The infrastructure-error.json record is the
  # authoritative signal for the platform.
  rm -f "$REWARD" "$REWARD_DETAILS" "$REWARD_TEXT"
  exit "$status"
}
trap infrastructure_failure ERR

for candidate_dir in /usr/local/bin /usr/bin /opt/node/bin /opt/node20/bin; do
  if [ -x "$candidate_dir/node" ] && [ -x "$candidate_dir/npm" ]; then
    node_major=$($candidate_dir/node -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)
    npm_major=$($candidate_dir/npm --version 2>/dev/null | cut -d. -f1 || true)
    if [ "$node_major" = 20 ] && [ "$npm_major" = 10 ]; then
      export PATH="$candidate_dir:$PATH"
      break
    fi
  fi
done

unset CODEX_API_KEY OPENAI_API_KEY OPENAI_BASE_URL OPENAI_API_BASE \
  GRADER_API_KEY GRADER_MODEL GRADER_REASONING_EFFORT REWARDKIT_REASONING_EFFORT \
  ANTHROPIC_API_KEY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

# The verifier must prove its own anti-cheating wiring before it reads or runs
# any candidate code. A broken detector (or a detector whose L1/L2 result is no
# longer wired to the grader's -1 branch) is an evaluator failure, never a
# candidate score.
if ! PYTHONDONTWRITEBYTECODE=1 python3 /tests/verifier/security_selftest.py \
  >"$LOGS/security-selftest.txt" 2>&1; then
  trap - ERR
  write_infrastructure_error "security_selftest" \
    "anti-cheating self-test failed; refusing to score the candidate"
  rm -f "$REWARD" "$REWARD_DETAILS" "$REWARD_TEXT"
  cat "$LOGS/security-selftest.txt" >&2
  exit 1
fi

apply_failed=0
if [ ! -f "$MODEL_PATCH" ] || [ ! -f "$MODEL_MANIFEST" ]; then
  apply_failed=1
elif [ -s "$MODEL_PATCH" ]; then
  # The verifier image's index stat-cache can be stale relative to the files
  # that Docker COPY materialized, which makes `git apply --index` reject an
  # otherwise valid patch with "does not match index". Refresh before applying.
  git -C "$REPO" update-index --refresh >/dev/null 2>&1 || true
  if git -C "$REPO" apply --check --binary --index "$MODEL_PATCH" >/dev/null 2>&1; then
    git -C "$REPO" apply --binary --index "$MODEL_PATCH" >/dev/null
  else
    apply_failed=1
  fi
fi

set +e
python3 /tests/verifier/verifier.py \
  --config "$CONFIG" \
  --repo "$REPO" \
  --ctrf-output "$CTRF_REPORT" \
  --hard-gate-output "$HARD_GATE_REPORT" \
  --apply-failed "$apply_failed"
verifier_exit_code=$?
set -e

if [ "$verifier_exit_code" -ne 0 ]; then
  trap - ERR
  if [ ! -s "$LOGS/infrastructure-error.json" ]; then
    write_infrastructure_error "verifier" "verifier aborted with invalid evaluation: exit $verifier_exit_code"
  fi
  # Only the anti-cheating branch may produce -1; an aborted verifier is an
  # infrastructure failure, so leave no reward sentinel behind.
  rm -f "$REWARD" "$REWARD_DETAILS" "$REWARD_TEXT"
  echo "Verifier aborted with invalid evaluation: exit $verifier_exit_code"
  exit "$verifier_exit_code"
fi

python3 /tests/grader.py gate \
  --config "$CONFIG" \
  --ctrf "$CTRF_REPORT" \
  --hard-gate "$HARD_GATE_REPORT" \
  --verifier "$VERIFIER_RESULT" \
  --output "$GATE_REWARD" \
  --apply-failed "$apply_failed"

python3 /tests/grader.py finalize \
  --config "$CONFIG" \
  --gate "$GATE_REWARD"

test -s "$CTRF_REPORT"
test -s "$REWARD"
test -s "$REWARD_DETAILS"
test -s "$REWARD_TEXT"
trap - ERR
exit 0

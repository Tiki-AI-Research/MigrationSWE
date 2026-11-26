#!/bin/bash
set -Eeuo pipefail

LOGS=/logs/verifier
REPORTS="$LOGS/reports"
MODEL_PATCH=/logs/artifacts/model.patch
REPO=/workspace/repo
EVAL_ROOT=/tmp/jssip-eval
EVAL_REPO="$EVAL_ROOT/repo"
PRIVATE="$EVAL_ROOT/private"
CONFIG=/tests/config.json
CTRF_REPORT="$LOGS/ctrf.json"
HARD_GATE_REPORT="$LOGS/hard-gate.json"
GATE_REWARD="$LOGS/gate-reward.json"
REWARD_DETAILS="$LOGS/reward-details.json"
REWARD="$LOGS/reward.json"

# The runtime image does not guarantee TMPDIR; hidden Node checks use it for
# caches.
export TMPDIR="${TMPDIR:-/tmp}"

mkdir -p "$REPORTS" "$LOGS"
rm -f "$CTRF_REPORT" "$REWARD" "$REWARD_DETAILS" "$LOGS/reward.txt" \
  "$HARD_GATE_REPORT" "$GATE_REWARD" "$LOGS/asset-integrity.json" \
  "$LOGS/sandbox-probe.json" "$LOGS/security-selfcheck.json" \
  "$LOGS/infrastructure-error.json"

write_infrastructure_error() {
  code=${1:-2}
  trap - ERR
  printf '%s\n' '{}' > "$REWARD"
  printf '{"status":"infrastructure_error","reward":null,"test":null,"migration_scope_complete_ratio":null,"f2p_pass_ratio":null,"p2p_pass_ratio":null,"cheating_detected":false,"errors":{"infrastructure_error":true,"agent_time_out":false,"build error":false,"compile error":false}}\n' > "$REWARD_DETAILS"
  printf '{"status":"INFRA_ERROR","exit_code":%s}\n' "$code" > "$LOGS/infrastructure-error.json"
  exit 2
}

on_err() {
  status=$?
  write_infrastructure_error "$status"
}
trap on_err ERR

lock_evaluator_tests() {
  # All evaluator material is root-only.  The one deliberate exception is the
  # hidden test payload text: candidates are allowed to inspect test.patch,
  # while expected.json, the reference parser and the gate sources stay hidden.
  chmod -R go-rwx /tests
  chmod 711 /tests /tests/verifier
  chmod 644 /tests/verifier/test.patch
}

# 1. Evaluator assets are verified before any contract, baseline, patch, or
# reference parser is consumed.  A bad manifest is a configuration error.
python3 /tests/verifier/asset_integrity.py \
  --assets /tests/assets \
  --tests-root /tests \
  --config "$CONFIG" \
  --out "$LOGS/asset-integrity.json"

# 2. Prove that the kernel can provide the private namespaces the verifier
# needs.  Without this check a missing capability would be misreported as a
# candidate grammar-build failure instead of an infrastructure error.
python3 /tests/verifier/sandbox.py > "$LOGS/sandbox-probe.json"

# 2b. Pinned security regressions for the evaluator itself.  These run before
# any candidate code and fail closed as an infrastructure error, so a future
# refactor cannot silently reintroduce a detector bypass or a false positive.
python3 /tests/verifier/test_security_selfcheck.py --quick \
  > "$LOGS/security-selfcheck.json"

# 3. Apply the candidate patch to the canonical repository.  This is the only
# tree whose diff is inspected by the gates.
apply_failed=0
if [ ! -f "$MODEL_PATCH" ] || [ ! -s "$MODEL_PATCH" ]; then
  apply_failed=1
elif ! git -C "$REPO" apply --check --binary "$MODEL_PATCH" >/dev/null 2>&1; then
  apply_failed=1
else
  git -C "$REPO" update-index --refresh >/dev/null 2>&1 || true
  if ! git -C "$REPO" apply --index --binary "$MODEL_PATCH" >/dev/null 2>&1; then
    apply_failed=1
  fi
fi

# 4. Preflight protected paths before applying the hidden payload.  A candidate
# that created the hidden harness path itself would make `git apply` fail; that
# is a protected-path violation (L1), not an evaluator infrastructure error.
protected_conflict=0
hidden_unavailable=0
if [ "$apply_failed" -eq 0 ]; then
  protected_rc=0
  if python3 /tests/verifier/protected_paths.py \
    --config "$CONFIG" \
    --repo "$REPO" \
    > "$LOGS/protected-path-check.json"; then
    protected_rc=0
  else
    protected_rc=$?
  fi
  if [ "$protected_rc" -eq 1 ]; then
    protected_conflict=1
    hidden_unavailable=1
  elif [ "$protected_rc" -ne 0 ]; then
    write_infrastructure_error 2
  fi
fi

# 5. Build an isolated evaluation workspace.  Hidden tests never enter the
# canonical candidate repository: they are applied to this root-owned copy and
# executed as an unprivileged identity.
rm -rf "$EVAL_ROOT"
mkdir -p "$EVAL_REPO" "$PRIVATE"
if [ "$apply_failed" -eq 0 ]; then
  tar -C "$REPO" \
    --exclude=.git \
    --exclude=./node_modules \
    -cf - . | tar -C "$EVAL_REPO" -xf -
  if [ -d "$REPO/node_modules" ]; then
    ln -s "$REPO/node_modules" "$EVAL_REPO/node_modules"
  fi
  if [ "$protected_conflict" -eq 0 ]; then
    if ! git -C "$EVAL_REPO" apply --whitespace=nowarn /tests/verifier/test.patch; then
      protected_rc=0
      if python3 /tests/verifier/protected_paths.py \
        --config "$CONFIG" \
        --repo "$REPO" \
        > "$LOGS/protected-path-check.json"; then
        protected_rc=0
      else
        protected_rc=$?
      fi
      if [ "$protected_rc" -eq 1 ]; then
        protected_conflict=1
        hidden_unavailable=1
      else
        write_infrastructure_error 2
      fi
    fi
  fi
  if [ "$hidden_unavailable" -eq 1 ]; then
    # Never execute a candidate-supplied file from the hidden harness path.
    rm -rf "$EVAL_REPO/src/test/migration-verifier"
  fi
  cp /tests/verifier/reference-grammar.js "$PRIVATE/reference-grammar.js"
  cp /tests/verifier/expected.json "$PRIVATE/expected.json"
  # The evaluation copy belongs to the unprivileged harness.  The private
  # answer directory stays root-owned and mode 700; do not chown EVAL_ROOT.
  chown -R -h 1001:1001 "$EVAL_REPO"
  lock_evaluator_tests
  # The unprivileged harness needs the linked dependency tree but not the
  # canonical source tree.
  chmod -R go+rX "$REPO/node_modules" 2>/dev/null || true
  chmod 711 "$EVAL_ROOT"
  chmod 700 "$PRIVATE"
else
  cp /tests/verifier/reference-grammar.js "$PRIVATE/reference-grammar.js"
  cp /tests/verifier/expected.json "$PRIVATE/expected.json"
  chmod 700 "$PRIVATE"
  chmod 711 "$EVAL_ROOT"
  lock_evaluator_tests
fi

# 6. Run the hidden checks and the composite hard gates.
python3 /tests/verifier/verifier.py \
  --config "$CONFIG" \
  --repo "$REPO" \
  --eval-repo "$EVAL_REPO" \
  --reference "$PRIVATE/reference-grammar.js" \
  --expected "$PRIVATE/expected.json" \
  --ctrf-output "$CTRF_REPORT" \
  --hard-gate-output "$HARD_GATE_REPORT" \
  --apply-failed "$apply_failed" \
  --hidden-unavailable "$hidden_unavailable"

python3 /tests/grader.py gate \
  --config "$CONFIG" \
  --ctrf "$CTRF_REPORT" \
  --hard-gate "$HARD_GATE_REPORT" \
  --output "$GATE_REWARD" \
  --apply-failed "$apply_failed"

python3 /tests/grader.py finalize \
  --config "$CONFIG" \
  --gate "$GATE_REWARD"

test -f "$CTRF_REPORT"
test -f "$REWARD"
test -f "$REWARD_DETAILS"
trap - ERR

status="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status", "infrastructure_error"))' "$REWARD_DETAILS" 2>/dev/null || printf '%s' 'infrastructure_error')"
case "$status" in
  verifier_success|verifier_failed) exit 0 ;;
  cheating_detected|hard_gate_failed) exit 1 ;;
  *) exit 2 ;;
esac

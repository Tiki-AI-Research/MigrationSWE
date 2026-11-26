#!/bin/sh
set -u

LOGS=/logs/verifier
mkdir -p "$LOGS"

python3 /tests/grader.py
status=$?

# Terminal fallback: every path must leave a final report behind.  The grader
# already traps its own failures; this covers launcher-level faults (missing
# interpreter, unreadable bundle) so a crashed evaluator is never mistaken for
# a scored candidate.
if [ ! -f "$LOGS/reward.json" ] || [ ! -f "$LOGS/reward-details.json" ]; then
  printf '{}\n' > "$LOGS/reward.json"
  cat > "$LOGS/reward-details.json" <<'JSON'
{
  "schema_version": "deepswe-reward-details/v2",
  "reward_mode": "VERIFIER_ONLY",
  "status": "infrastructure_error",
  "cheating_detected": false,
  "test": null,
  "migration_scope_complete_ratio": null,
  "f2p_pass_ratio": null,
  "p2p_pass_ratio": null,
  "reward": null,
  "errors": {
    "infrastructure_error": true,
    "agent_time_out": false,
    "build error": false,
    "compile error": false
  }
}
JSON
  exit 2
fi

exit "$status"

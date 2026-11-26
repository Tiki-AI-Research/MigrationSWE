#!/bin/sh
set -eu

logs=/logs/verifier
mkdir -p "$logs"

# Keep Harbor's result parser informed even when Python fails before the
# grader can enter its own infrastructure-error handler (for example, an
# import or interpreter startup failure).
on_exit() {
    status=$?
    trap - EXIT
    if [ "$status" -ne 0 ] && [ ! -s "$logs/reward.json" ] && [ ! -s "$logs/reward.txt" ]; then
        rm -f "$logs/reward-details.json" "$logs/ctrf.json"
        printf '%s\n' '{"reward": 0}' > "$logs/reward.json"
        printf '%s\n' '0' > "$logs/reward.txt"
    fi
    exit "$status"
}
trap on_exit EXIT

python3 /tests/grader.py

#!/bin/sh
# Target-side probe harness for BATCH-F2P-0002.
#
# All potentially long-running commands are wrapped in an explicit `timeout`
# and run offline against the locked workspace so the harness can never block
# indefinitely.
set -eu

REPO="${1:-/workspace/repo}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ASSET_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
DISCOVER="$ASSET_ROOT/shared/integration/crate_discovery.py"

# The migration contract only fixes the language, not the crate location, so
# discover the native Rust crate and install the probe there.
CRATE_INFO="$(python3 "$DISCOVER" --repo "$REPO")" || {
  echo "no native Rust library crate found in $REPO" >&2
  exit 1
}
CRATE="$(printf '%s' "$CRATE_INFO" | python3 -c 'import json,sys; print(json.load(sys.stdin)["dir"])')"
CRATE_LIB="$(printf '%s' "$CRATE_INFO" | python3 -c 'import json,sys; print(json.load(sys.stdin)["lib"])')"

# The target Rust public interface is fixed by the contract (see
# instruction.md), so the evaluator-owned postcondition suite MUST compile
# against the candidate crate and its assertions MUST pass.  Neither a binding
# mismatch nor an assertion failure may fall back to the candidate's own suite,
# which cannot prove the contract behaviour.
mkdir -p "${CRATE}/tests"
# Always remove the installed evaluator test file so it cannot leak into the
# candidate's workspace and contaminate later batches' native test runs.
trap 'rm -f "${CRATE}/tests/f2p_0002.rs"' EXIT INT TERM
python3 - "$SCRIPT_DIR/tests/f2p_0002.rs" "$CRATE_LIB" "${CRATE}/tests/f2p_0002.rs" <<'PY'
import sys

source, lib, dest = sys.argv[1], sys.argv[2], sys.argv[3]
with open(source, encoding="utf-8") as handle:
    text = handle.read()
with open(dest, "w", encoding="utf-8") as handle:
    handle.write(text.replace("stepperize_core", lib))
PY

set +e
PROBE_OUT="$(timeout 1500 cargo test --manifest-path "${CRATE}/Cargo.toml" --workspace --offline --test f2p_0002 2>&1)"
PROBE_RC=$?
set -e
# The integration adapter uses Cargo's per-test records to attribute a failure
# only to its owning observation.  Emit them on both success and failure.
printf '%s\n' "$PROBE_OUT"
if [ "$PROBE_RC" -eq 0 ]; then
  exit 0
fi
# A binding mismatch means the crate does not expose the required public
# interface; an assertion failure means the behaviour is wrong.  Both are FAIL.
echo "postcondition suite did not pass against the candidate crate" >&2
exit 1

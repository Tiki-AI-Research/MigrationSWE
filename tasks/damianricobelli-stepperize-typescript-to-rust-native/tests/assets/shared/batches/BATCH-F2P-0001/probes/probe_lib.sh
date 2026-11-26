#!/bin/sh
# Shared probe helpers for BATCH-F2P-0001 (F2P target-postcondition batch).
#
# Every potentially long-running command is wrapped in an explicit `timeout`
# so the harness can never block forever. The default budget matches the
# provided target harness (run_tests.py uses timeout=1500s).

F2P_TIMEOUT_SECS="${F2P_TIMEOUT_SECS:-1500}"

# run_cargo_test <repo-dir>
# Build and test the Rust workspace offline under an explicit timeout.
run_cargo_test() {
  repo="$1"
  ( cd "$repo" && timeout "$F2P_TIMEOUT_SECS" env \
      CARGO_NET_OFFLINE=true CARGO_HOME="${CARGO_HOME:-/usr/local/cargo}" \
      cargo test --workspace --locked --offline )
}

# cargo_manifest_has_dependencies <Cargo.toml> [section]
# Print "yes" when the (default [dependencies]) table has entries, else "no".
cargo_manifest_has_dependencies() {
  python3 - "$1" "${2:-[dependencies]}" <<'PY'
import sys
path, want = sys.argv[1], sys.argv[2]
section = None
entries = []
with open(path, encoding="utf-8") as handle:
    for raw in handle:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
            continue
        if section == want:
            entries.append(line)
print("yes" if entries else "no")
PY
}

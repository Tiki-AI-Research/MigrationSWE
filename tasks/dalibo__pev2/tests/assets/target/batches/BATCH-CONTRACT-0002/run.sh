#!/bin/sh
# Target-side CONTRACT probe runner for BATCH-CONTRACT-0002.
# Every potentially long-running command is wrapped in an explicit `timeout`.
# The build is fully offline (`--offline`, `CARGO_NET_OFFLINE=true`); no
# privileged commands and no permanently blocking cases are executed.
set -eu

REPO_ROOT="${REPO_ROOT:-/workspace/repo}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export REPO_ROOT
export CARGO_HOME="${CARGO_HOME:-/opt/migration/dependencies/cargo}"
export CARGO_NET_OFFLINE=true

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/src"

cat >"$WORK/Cargo.toml" <<EOF
[package]
name = "contract-probe"
version = "0.0.0"
edition = "2021"

[dependencies]
pev2-core = { path = "$REPO_ROOT/crates/pev2-core" }
serde_json = "1"
EOF

cp "$SCRIPT_DIR/contract-target.rs" "$WORK/src/main.rs"

timeout 900 cargo build --quiet --offline --manifest-path "$WORK/Cargo.toml"
PROBE_OUT="$(timeout 300 cargo run --quiet --offline --manifest-path "$WORK/Cargo.toml")"

PROBE_OUT="$PROBE_OUT" REPO_ROOT="$REPO_ROOT" timeout 300 python3 - <<'PY'
import hashlib
import json
import os

repo_root = os.environ["REPO_ROOT"]
roots = ["src/services/__tests__", "src/__tests__", "tests/e2e"]
files = []
for root in roots:
    base = os.path.join(repo_root, root)
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            files.append(os.path.join(dirpath, name))
files.sort()
digest = hashlib.sha256()
for path in files:
    rel = os.path.relpath(path, repo_root)
    with open(path, "rb") as handle:
        file_digest = hashlib.sha256(handle.read()).hexdigest()
    digest.update(f"{rel}\n{file_digest}\n".encode())

result = json.loads(os.environ["PROBE_OUT"])
result["CONTRACT-035"] = digest.hexdigest()
print(json.dumps(result))
PY

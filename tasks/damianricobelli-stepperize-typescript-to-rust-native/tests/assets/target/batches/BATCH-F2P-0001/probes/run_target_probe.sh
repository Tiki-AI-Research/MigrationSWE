#!/bin/sh
# BATCH-F2P-0001 target-side probe.
#
# Declares PASS only after the target postconditions of every owned observation
# have really been executed against the Rust target. The probe:
#   1. verifies the crate exists and is self-contained (R002, R028);
#   2. verifies production sources contain no subprocess/FFI/dynamic-loading
#      delegation (R027, R028);
#   3. removes the TypeScript implementation and re-runs the workspace tests to
#      prove the responsibilities are implemented in Rust (R002, R027);
#   4. runs the F2P postcondition integration tests (R005, R034, R035, R033,
#      R041, R040, R042).
#
# Every cargo invocation is wrapped in an explicit `timeout`; the harness can
# never wait forever. No privileged or permanently blocking cases are used.

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BATCH_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
ASSET_ROOT="$(CDPATH= cd -- "$BATCH_DIR/../../.." && pwd)"

if [ -f "$ASSET_ROOT/shared/batches/BATCH-F2P-0001/probes/probe_lib.sh" ]; then
  # shellcheck source=/dev/null
  . "$ASSET_ROOT/shared/batches/BATCH-F2P-0001/probes/probe_lib.sh"
else
  F2P_TIMEOUT_SECS="${F2P_TIMEOUT_SECS:-1500}"
  run_cargo_test() {
    ( cd "$1" && timeout "$F2P_TIMEOUT_SECS" env CARGO_NET_OFFLINE=true \
        cargo test --workspace --locked --offline )
  }
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
fi

REPO="${MIGRATION_REPO:-/workspace/repo}"
TEST_FILE="$BATCH_DIR/crates/stepperize-core/tests/f2p_postconditions.rs"
DISCOVER="$ASSET_ROOT/shared/integration/crate_discovery.py"

fail() {
  echo "F2P target probe FAILED: $1" >&2
  exit 1
}

# The migration contract only requires a native Rust crate somewhere in the
# repository; discover it instead of assuming the reference layout.
CRATE_INFO="$(python3 "$DISCOVER" --repo "$REPO")" \
  || fail "no native Rust library crate found in the repository"
CRATE="$(printf '%s' "$CRATE_INFO" | python3 -c 'import json,sys; print(json.load(sys.stdin)["dir"])')"
CRATE_REL="$(printf '%s' "$CRATE_INFO" | python3 -c 'import json,sys; print(json.load(sys.stdin)["rel"])')"
CRATE_LIB="$(printf '%s' "$CRATE_INFO" | python3 -c 'import json,sys; print(json.load(sys.stdin)["lib"])')"
CRATE_SOURCE="$(printf '%s' "$CRATE_INFO" | python3 -c 'import json,sys; print(json.load(sys.stdin)["source"])')"

[ -f "$CRATE/Cargo.toml" ] || fail "missing $CRATE/Cargo.toml (no native Rust crate)"
[ -f "$CRATE_SOURCE" ] || fail "missing crate library root"

# Supporting Rust dependencies and build configuration are allowed. Runtime
# delegation remains forbidden and is checked below and by the hard gates.

# R027/R028: no subprocess, FFI, RPC, or dynamic loading in production sources.
# The scan mirrors the verifier detectors: it tolerates inert whitespace
# (including line breaks) around Rust paths and call delimiters, treats every
# C-compatible ABI string as the same FFI boundary, and walks the whole crate
# rather than only ``src``.  A module pulled in through
# ``include!(concat!(...))`` therefore cannot hide a delegation sink from the
# probe while the detectors stay agnostic about the file's suffix. Only Rust
# sources and their textual includes are code in this native crate: a crate
# rooted at the repository root also contains retained JS dependency locks.
if python3 - "$CRATE" "$CRATE_SOURCE" <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
pattern = re.compile(
    r'std\s*::\s*process'
    r'|process\s*::\s*Command'
    r'|extern\s*"\s*(?:C|system|stdcall|cdecl|fastcall|thiscall|win64|sysv64)\s*"'
    r'|libloading|dlopen|napi|neon|wasm_bindgen|js_sys'
    # Raw process-launch primitives: a symbol scan alone misses a candidate that
    # launches the source runtime through a system call (no ``std::process`` /
    # ``dlopen`` symbol ever appears).  Keep the probe and the verifier detectors
    # covering the same primitive family so neither is the shared blind spot.
    r'|(?:core\s*::\s*arch\s*::\s*)?(?:global_asm|naked_asm|asm)\s*!'
    r'|\bsyscall\b|\bsysenter\b|\becall\b|\bsvc\b'
    r'|libc\s*::\s*syscall'
    r'|\b(?:execveat|execve|execvpe|execvp|execv|execlp|execle|execl|posix_spawnp|posix_spawn)\b'
    r'|\b(?:SYS_execveat|SYS_execve|__NR_execveat|__NR_execve)\b'
    r'|CommandExt',
    re.IGNORECASE,
)
skip = {"target", ".git", "node_modules"}
include_macro = re.compile(r'\binclude\s*!\s*([({\[])')
module_path = re.compile(r'#\s*\[\s*path\s*=\s*(?:r\#*)?"([^"]+)"')
string_literal = re.compile(r'(?:r\#*)?"([^"]*)"')


def included_paths(path, text):
    # Follow include!("file") and include!(concat!("file", ".inc")) as
    # well as #[path = "file"], including recursively included non-.rs files.
    for match in module_path.finditer(text):
        yield path.parent / match.group(1)
    for match in include_macro.finditer(text):
        opener = match.group(1)
        closer = {"(": ")", "{": "}", "[": "]"}[opener]
        depth, end = 1, match.end()
        while end < len(text) and depth:
            if text[end] == opener:
                depth += 1
            elif text[end] == closer:
                depth -= 1
            end += 1
        arguments = text[match.end():end - 1]
        arguments = re.sub(
            r'env\s*!\s*\(\s*"CARGO_MANIFEST_DIR"\s*\)',
            lambda _: '"' + str(root.resolve()) + '"',
            arguments,
        )
        pieces = string_literal.findall(arguments)
        if pieces:
            yield path.parent / "".join(pieces)


queue = sorted(root.rglob("*.rs")) + [Path(sys.argv[2])]
seen = set()
while queue:
    path = queue.pop()
    if path.is_symlink() or not path.is_file():
        continue
    path = path.resolve()
    if not path.is_relative_to(root.resolve()):
        continue
    if skip & set(path.relative_to(root.resolve()).parts) or path in seen:
        continue
    seen.add(path)
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    queue.extend(included_paths(path, text))
    if pattern.search(text):
        print(path)
        sys.exit(1)
sys.exit(0)
PY
then
  :
else
  fail "production sources delegate through subprocess/FFI/dynamic loading"
fi

# R002/R027: removing the TypeScript implementation must not affect build/test.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo"
( cd "$REPO" && tar --exclude=./target --exclude=./.git -cf - . ) \
  | ( cd "$TMP/repo" && tar -xf - )
# Remove the TypeScript implementation only.  The candidate may legitimately
# place its Rust library under packages/core/src, so delete *.ts files rather
# than the whole directory.
find "$TMP/repo/packages/core/src" -type f -name '*.ts' -delete 2>/dev/null || true
# The target Rust public interface is fixed by the contract (see
# instruction.md), so the evaluator-owned postcondition suite MUST compile
# against the candidate crate and its assertions MUST pass.  Neither a binding
# mismatch nor an assertion failure may fall back to the candidate's own suite,
# which cannot prove the contract behaviour.
CRATE_TMP="$TMP/repo/$CRATE_REL"
mkdir -p "$CRATE_TMP/tests"
python3 - "$TEST_FILE" "$CRATE_LIB" "$CRATE_TMP/tests/f2p_postconditions.rs" <<'PY'
import sys

source, lib, dest = sys.argv[1], sys.argv[2], sys.argv[3]
with open(source, encoding="utf-8") as handle:
    text = handle.read()
with open(dest, "w", encoding="utf-8") as handle:
    handle.write(text.replace("stepperize_core", lib))
PY
set +e
PROBE_OUT="$(cd "$TMP/repo" && timeout "$F2P_TIMEOUT_SECS" cargo test --manifest-path "$CRATE_TMP/Cargo.toml" --offline --test f2p_postconditions 2>&1)"
PROBE_RC=$?
set -e
# The integration harness maps evaluator-owned test names back to their
# individual observations.  Preserve cargo's per-test output on both success
# and failure; without it a single assertion failure would incorrectly mark
# every observation in this batch as failed.
printf '%s\n' "$PROBE_OUT"
if [ "$PROBE_RC" -eq 0 ]; then
  echo "F2P target probe PASSED"
  exit 0
fi
# A binding mismatch means the crate does not expose the required public
# interface; an assertion failure means the behaviour is wrong.  Both are FAIL.
fail "postcondition suite did not pass against the candidate crate"

echo "F2P target probe PASSED"

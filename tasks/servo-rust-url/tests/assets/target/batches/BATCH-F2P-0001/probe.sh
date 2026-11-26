#!/bin/sh
# BATCH-F2P-0001 target-side probe driver.
# Runs the shared semantic predicate against the reference target root.
# Usage: probe.sh [ROOT] [SIDE]
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${1:-/input/target}
SIDE=${2:-target}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Prohibit any delegation to the Rust toolchain: cargo/rustc stubs fail hard.
mkdir -p "$WORK/bin"
printf '#!/bin/sh\nexit 1\n' > "$WORK/bin/cargo"
printf '#!/bin/sh\nexit 1\n' > "$WORK/bin/rustc"
chmod +x "$WORK/bin/cargo" "$WORK/bin/rustc"

cp "$HERE/probe_main.go" "$WORK/probe_main.go"
cat > "$WORK/go.mod" <<EOF
module f2pprobe

go 1.23

require github.com/servo/rust-url v0.0.0

replace github.com/servo/rust-url => $ROOT
EOF

if ( cd "$WORK" && PATH="$WORK/bin:$PATH" PROBE_SIDE="$SIDE" \
      GOPROXY=off GOTOOLCHAIN=local CGO_ENABLED=0 go run . ) > "$WORK/runtime.json" 2>"$WORK/runtime.err"; then
  cat "$WORK/runtime.json"
else
  printf '{"side":"%s","results":[' "$SIDE"
  FIRST=1
  for ID in F2P-001 F2P-002 F2P-003 F2P-004 F2P-005 F2P-006 F2P-007 F2P-008 \
            F2P-009 F2P-010 F2P-011 F2P-014 F2P-015 F2P-016 F2P-017 F2P-018 \
            F2P-019 F2P-020 F2P-021 F2P-022 F2P-023 F2P-024; do
    [ "$FIRST" -eq 1 ] || printf ','
    FIRST=0
    printf '{"observation_id":"%s","status":"FAIL","detail":"target capability absent in %s environment"}' "$ID" "$SIDE"
  done
  printf ']}\n'
fi

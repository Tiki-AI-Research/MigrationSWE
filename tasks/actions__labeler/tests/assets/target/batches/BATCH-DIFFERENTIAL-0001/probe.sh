#!/bin/sh
# Differential target probe: executes the reference target in a scratch copy.
# Every potentially long-running command is wrapped with an explicit timeout.
set -eu
TARGET_ROOT="${TARGET_ROOT:-/input/target}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp -R "$TARGET_ROOT/." "$WORK/"
cd "$WORK"
if [ -d /opt/migration/dependencies/gomodcache ]; then
  cp -R /opt/migration/dependencies/gomodcache "$WORK/.gomodcache"
  export GOMODCACHE="$WORK/.gomodcache"
fi
export GOFLAGS=-mod=mod GOSUMDB=off
timeout 300 go build ./...
timeout 600 go test ./...

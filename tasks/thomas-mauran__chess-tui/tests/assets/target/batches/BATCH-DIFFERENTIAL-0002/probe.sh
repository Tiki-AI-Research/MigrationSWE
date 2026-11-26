#!/bin/sh
# DIFFERENTIAL target probe: observation DIFF-033, requirement R037.
# Executed on the reference target (/input/target during authoring).
#
# The target side carries the complete Go implementation as an uncommitted
# working-tree patch relative to the task baseline revision. This probe
# asserts that (a) the baseline revision is intact and the checkout was not
# reset, and (b) the working tree actually contains the migrated Go
# implementation, so the collected patch is non-empty.
#
# Usage (all forms accepted):
#   probe.sh [repo_path]
#   probe.sh --repo <repo_path> [--root <root>] [--side target] [--out <file>]
set -u

REPO="${REPO:-/input/target}"
OUT=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo|-r) REPO="$2"; shift 2 ;;
    --root)    shift 2 ;;
    --side)    shift 2 ;;
    --out|-o)  OUT="$2"; shift 2 ;;
    -h|--help) shift ;;
    -*)        shift ;;
    *)         REPO="$1"; shift ;;
  esac
done

OBS="DIFF-033"
CASE="baseline-manifests-intact"
ARTIFACTS="Cargo.toml Cargo.lock src/lib.rs src/main.rs examples/sample.pgn"
# The deliverable is a single Go module that exposes the migrated
# responsibilities under the required internal packages (instruction.md
# "Goal").  The check verifies that contract at the package level: it does not
# require any particular source file name, because the task fixes the module
# path and package layout, not the file layout.
MODULE_PATH="github.com/thomas-mauran/chess-tui"
REQUIRED_PACKAGES="config pgn chess tcp uci lichess skin constants"

status="PASS"
detail="baseline-intact-go-module-present"
for a in $ARTIFACTS; do
  if [ ! -e "$REPO/$a" ]; then
    status="FAIL"
    detail="missing-baseline-artifact:$a"
    break
  fi
done

if [ "$status" = "PASS" ]; then
  if [ ! -f "$REPO/go.mod" ]; then
    status="FAIL"
    detail="missing-go-module"
  elif ! grep -q "module $MODULE_PATH" "$REPO/go.mod"; then
    status="FAIL"
    detail="wrong-module-path"
  fi
fi

if [ "$status" = "PASS" ]; then
  if ! grep -lq "^package main" "$REPO"/*.go 2>/dev/null; then
    status="FAIL"
    detail="missing-main-package"
  fi
fi

if [ "$status" = "PASS" ]; then
  for pkg in $REQUIRED_PACKAGES; do
    if ! ls "$REPO/internal/$pkg"/*.go >/dev/null 2>&1; then
      status="FAIL"
      detail="missing-internal-package:$pkg"
      break
    fi
  done
fi

if [ "$status" = "PASS" ] && [ -d "$REPO/.git" ]; then
  # Only the declared baseline artifacts (see baseline-artifacts.json) must stay
  # present and un-mutated so the collected patch is expressed relative to the
  # task baseline revision.  A legitimate migration may edit *other* tracked
  # files (README, .gitignore) or add new files, which the collected patch
  # carries; requiring the whole working tree to be clean would reject those
  # legal migrations.  A reset that changes a baseline artifact is still caught.
  if ! ( cd "$REPO" && git diff --quiet --exit-code -- $ARTIFACTS && git diff --cached --quiet --exit-code -- $ARTIFACTS ); then
    status="FAIL"
    detail="baseline-artifact-dirty"
  fi
fi

json=$(printf '{"observation_id":"%s","case":"%s","requirement_id":"R037","result":"%s","detail":"%s"}' \
  "$OBS" "$CASE" "$status" "$detail")
printf '%s\n' "$json"
if [ -n "$OUT" ]; then printf '%s\n' "$json" > "$OUT"; fi

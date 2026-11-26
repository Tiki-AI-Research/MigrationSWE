#!/bin/sh
set -eu
REPO="/workspace/repo"
PATCH_DIR="$(dirname "$0")"
git -C "$REPO" apply --binary "$PATCH_DIR/solution.patch"
git -C "$REPO" add -A
git -C "$REPO" commit -m "solution: migrate sharing from express 4 to fastify 4"

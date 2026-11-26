#!/bin/sh
# Keep the legacy argv contract; the third (injected Go fixture) argument is no
# longer needed because candidate-internal interfaces are not public contracts.
set -eu
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "$here/../../integration/public_behavior.py" --batch BATCH-F2P-0002 --repo "${1:-/workspace/repo}" --out "${2:-/logs/result.json}"

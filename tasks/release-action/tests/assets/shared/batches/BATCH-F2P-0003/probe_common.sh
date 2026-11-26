#!/bin/sh
# Public behavior owns the assertions. Preserve the callable probe_run interface
# used by the source/target batch launchers, including single-observation runs.
PROBE_BATCH="BATCH-F2P-0003"
PROBE_OBSERVATIONS="F2P-049 F2P-050 F2P-051 F2P-052 F2P-053 F2P-054 F2P-055 F2P-056 F2P-057 F2P-058 F2P-059 F2P-060 F2P-061 F2P-062 F2P-063 F2P-064 F2P-065 F2P-066 F2P-067 F2P-068 F2P-069 F2P-070 F2P-071 F2P-072"
probe_run() {
  python3 "$PROBE_ASSET_ROOT/shared/integration/public_behavior.py" --batch "$PROBE_BATCH" --repo "$1" --observation "$2"
}

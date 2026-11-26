#!/bin/sh
set -eu
FT_SIDE=target
export FT_SIDE
. "$(dirname "$0")/../../../shared/batches/BATCH-F2P-0002/lib.sh"
report F2P-040 f2p_040

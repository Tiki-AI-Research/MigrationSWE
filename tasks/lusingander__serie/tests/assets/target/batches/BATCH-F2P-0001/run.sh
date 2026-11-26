#!/bin/sh
set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for observation in F2P-001 F2P-002 F2P-003 F2P-004 F2P-005 F2P-006 F2P-007 F2P-008 F2P-009 F2P-010 F2P-011 F2P-012 F2P-013 F2P-014 F2P-015 F2P-016 F2P-017 F2P-018 F2P-019 F2P-020 F2P-021 F2P-022 F2P-023 F2P-024; do
  "$here/$observation.sh" || true
done
exit 0

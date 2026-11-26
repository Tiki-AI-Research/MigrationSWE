#!/bin/sh
# BATCH-F2P-0001 repository change-set policy check (F2P-012 / F2P-013).
# Compares the reference target working tree against the source baseline and
# verifies the change set is limited to the added Go module (go.mod + .go files
# under the migrated packages) and that protected conformance inputs are
# byte-identical.
# Usage: policy_check.sh [SOURCE_BASELINE] [TARGET_ROOT]
set -u
SOURCE=${1:-/input/source}
TARGET=${2:-/input/target}
python3 - "$SOURCE" "$TARGET" <<'PY'
import hashlib
import json
import os
import sys

src, tgt = sys.argv[1], sys.argv[2]


def snapshot(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != ".git"]
        for f in fn:
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, root)
            with open(p, "rb") as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


s = snapshot(src)
t = snapshot(tgt)
added = sorted(set(t) - set(s))
removed = sorted(set(s) - set(t))
changed = sorted(r for r in set(s) & set(t) if s[r] != t[r])

allowed_dirs = ("url/", "idna/", "percentencoding/", "formurlencoded/", "internal/")


def allowed(p):
    if p == "go.mod":
        return True
    return p.endswith(".go") and p.startswith(allowed_dirs)


bad_added = [p for p in added if not allowed(p)]
unexpected = bad_added + removed + changed

protected = [
    "url/tests/urltestdata.json",
    "url/tests/setters_tests.json",
    "idna/tests/IdnaTestV2.txt",
    "idna/tests/punycode_tests.json",
    "idna/tests/bad_punycode_tests.json",
]
protected_ok = all(s.get(p) == t.get(p) for p in protected if p in s or p in t)

ok = not unexpected and protected_ok
detail = "added go.mod + .go under migrated packages; %d source files modified" % len(unexpected)
res = [
    {"observation_id": "F2P-012", "status": "PASS" if ok else "FAIL", "detail": detail},
    {"observation_id": "F2P-013", "status": "PASS" if ok else "FAIL",
     "detail": "change set limited to added Go module; protected inputs byte-identical"},
]
print(json.dumps({"side": "target", "check": "repository-change-set", "results": res}, indent=2))
PY

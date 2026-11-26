#!/bin/bash
set -euo pipefail

cd /workspace/repo
CONTRACT=/solution/contract.json
PATCH=/solution/solution.patch

python3 - "$CONTRACT" "$PATCH" <<'PY'
import hashlib, json, pathlib, subprocess, sys
contract = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
patch = pathlib.Path(sys.argv[2])
expected_tree = contract['replay_base']['tree']
actual_tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], text=True).strip()
if actual_tree != expected_tree:
    raise SystemExit(f'reference solution base tree mismatch: {actual_tree} != {expected_tree}')
actual_patch = hashlib.sha256(patch.read_bytes()).hexdigest()
expected_patch = contract['reference_solution']['sha256']
if actual_patch != expected_patch:
    raise SystemExit(f'reference solution patch mismatch: {actual_patch} != {expected_patch}')
PY

git apply --check --binary "$PATCH"
git apply --binary "$PATCH"
git apply --reverse --check --binary "$PATCH"
git apply --reverse --binary "$PATCH"
git apply --check --binary "$PATCH"
git apply --binary "$PATCH"

branch=feature/solution
if git show-ref --verify --quiet "refs/heads/$branch"; then
  echo "reference solution branch already exists: $branch" >&2
  exit 1
fi
git checkout -b "$branch"
git add -A
if git diff --cached --quiet; then
  echo "reference solution patch produced no changes" >&2
  exit 1
fi
git -c user.name="oracle" -c user.email="oracle@local" commit -q --no-verify -m "Apply reference solution"
if git status --porcelain | grep -q .; then
  echo "reference solution left an uncommitted working tree" >&2
  exit 1
fi

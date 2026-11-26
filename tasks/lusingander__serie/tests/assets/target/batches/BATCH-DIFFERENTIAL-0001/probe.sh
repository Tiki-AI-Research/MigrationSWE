#!/bin/sh
# Run the same evaluator binding as the integration runner.
# Usage: probe.sh <repository-dir> [junit-report]
set -eu

repo=${1:?usage: probe.sh <repository-dir> [junit-report]}
report=${2:-}

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

exec python3 - "$repo" "$report" "$here" <<'PY'
import os
from pathlib import Path
import shutil
import subprocess
import sys

repo = Path(sys.argv[1]).resolve()
report = Path(sys.argv[2]).resolve() if sys.argv[2] else None
here = Path(sys.argv[3])
sys.path.insert(0, str(here.parents[2] / "shared" / "integration"))
import serie_bind

contract = serie_bind.build_contract_repo(repo)
try:
    serie_bind.write_test(contract, "zz_integration_diff_test.go", here / "core" / "diffbatch0001_test.go")
    environment = dict(os.environ)
    environment.setdefault("CGO_ENABLED", "0")
    environment.setdefault("GOPROXY", "off")
    environment.setdefault("GOSUMDB", "off")
    environment.setdefault("GOTOOLCHAIN", "local")
    environment.setdefault("GOFLAGS", "-mod=readonly")
    environment.setdefault("GOCACHE", str(contract.parent / "gocache"))
    environment.setdefault("GOMODCACHE", str(contract.parent / "gomodcache"))
    arguments = ["-count=1", "-run", "^TestDiffB0001", "./" + serie_bind.SHIM_DIRNAME]
    command = ["go", "test", *arguments]
    if report is not None:
        command = ["gotestsum", "--junitfile=" + str(report), "--format=standard-verbose", "--", *arguments]
    result = subprocess.run(command, cwd=contract, env=environment)
finally:
    shutil.rmtree(contract.parent)
raise SystemExit(result.returncode)
PY

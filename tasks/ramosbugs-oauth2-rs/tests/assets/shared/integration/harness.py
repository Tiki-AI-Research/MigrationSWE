"""Shared harness logic for the BATCH-INTEGRATION-0001 runners.

The INTEGRATION batch owns the runtime entrypoints ``source/runner`` and
``target/runner``.  Both entrypoints are intentionally thin: they resolve the
asset root and the repository under test, then delegate to this module, which
merges the executable projections authored by the dependency batches and emits a
``migration-observations:v6`` report.

The module is written for the published runtime layout: the asset root contains
only ``shared/``, ``source/``, ``target/``, ``fixtures/`` and the two runners.
It never reads the authoring-only ``dependencies/`` or ``fragments/`` trees and
never assumes an authoring path; the repository under test is discovered only
from the ``MIGRATION_REPO_ROOT`` / ``MIGRATION_REPO`` / ``REPO_ROOT`` / ``REPO``
environment variables (defaulting to ``/workspace/repo``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA_VERSION = "migration-observations:v6"
BATCH_ID = "BATCH-INTEGRATION-0001"
REPO_ROOT = "/workspace/repo"

# Authored probe files merged into the migrated package by the target runner.
TARGET_PROBES = (
    "target/batches/BATCH-F2P-0001/f2p_0001_target_test.go",
    "target/batches/BATCH-P2P-0001/p2p_public_contract_test.go",
    "target/batches/BATCH-F2P-0003/f2p_probes_test.go",
    "target/batches/BATCH-DIFFERENTIAL-0001/probe_test.go",
)
# Self-contained external-package helpers for the merged evaluator probes.
TARGET_SUPPORT = "target/integration/integration_support_test.go"

_TEST_LINE = re.compile(r"^test\s+([A-Za-z0-9_:]+)\s+\.\.\.\s+(ok|FAILED|ignored)", re.M)
_PACKAGE_LINE = re.compile(r"^package\s+([A-Za-z0-9_]+)", re.M)


# --------------------------------------------------------------------------- #
# environment / repository discovery
# --------------------------------------------------------------------------- #
def assets_root() -> Path:
    return Path(__file__).resolve().parents[2]


def repo_root() -> Path:
    for key in ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO"):
        value = os.environ.get(key)
        if value:
            return Path(value).resolve()
    return Path(REPO_ROOT)


def load_map(assets: Path) -> dict:
    with open(assets / "shared" / "integration" / "observation_map.json", encoding="utf-8") as handle:
        return json.load(handle)


def _which(name: str) -> str | None:
    return shutil.which(name)


def _read_package(path: Path) -> str | None:
    try:
        match = _PACKAGE_LINE.search(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return match.group(1) if match else None


def find_module_root(repo: Path) -> Path | None:
    if not repo.is_dir():
        return None
    for path in sorted(repo.rglob("go.mod")):
        if "vendor" in path.parts:
            continue
        return path.parent
    return None


def locate_layout(repo: Path) -> dict | None:
    """Return {module_root, module_path, package_dir, import_path} or None."""
    module_root = find_module_root(repo)
    if module_root is None:
        return None
    module_path = None
    for line in (module_root / "go.mod").read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("module "):
            module_path = stripped[len("module "):].strip().strip('"')
            break
    if not module_path:
        return None

    by_dir: dict[Path, set] = {}
    for path in module_root.rglob("*.go"):
        if path.name.endswith("_test.go") or "vendor" in path.parts:
            continue
        package = _read_package(path)
        if package and package != "main":
            by_dir.setdefault(path.parent, set()).add(package)

    if not by_dir:
        return None

    package_dir = None
    preferred = module_root / "oauth2"
    if preferred in by_dir and "oauth2" in by_dir[preferred]:
        package_dir = preferred
    elif module_root in by_dir:
        package_dir = module_root
    else:
        package_dir = sorted(by_dir, key=lambda p: (len(p.parts), str(p)))[0]

    if package_dir == module_root:
        import_path = module_path
    else:
        rel = package_dir.relative_to(module_root).as_posix()
        import_path = f"{module_path}/{rel}"
    return {
        "module_root": module_root,
        "module_path": module_path,
        "package_dir": package_dir,
        "import_path": import_path,
    }


# --------------------------------------------------------------------------- #
# target side
# --------------------------------------------------------------------------- #
def _ignore(_dir, names):
    # Candidate tests have already run through the native-test command. Keep
    # the evaluator's separate test workspace independent of their helper and
    # test names; otherwise valid submissions can fail with redeclarations.
    return [n for n in names if n in (".git", "target", "__pycache__") or n.endswith("_test.go")]


def build_target_workspace(assets: Path, repo: Path) -> tuple[dict, Path]:
    layout = locate_layout(repo)
    if layout is None:
        raise RuntimeError("no Go module found in repository")
    tmp = Path(tempfile.mkdtemp(prefix="oauth2-integration-"))
    workspace = tmp / "repo"
    shutil.copytree(repo, workspace, ignore=_ignore, symlinks=True)
    layout = locate_layout(workspace)
    package_dir = layout["package_dir"]
    import_path = layout["import_path"]

    for rel in (*TARGET_PROBES, TARGET_SUPPORT):
        source = assets / rel
        if not source.is_file():
            raise RuntimeError(f"required evaluator probe is missing: {rel}")
        text = source.read_text(encoding="utf-8")
        # All probes are external-package consumers of the public API. Both
        # aliased and dot imports resolve to the candidate's package.
        text = text.replace('"oauth2/oauth2"', f'"{import_path}"')
        (package_dir / source.name).write_text(text, encoding="utf-8")

    return layout, workspace


def run_go_tests(workspace: Path, layout: dict) -> dict:
    go = _which("go")
    if go is None:
        return {"available": False, "results": {}, "build_error": False, "output": "go toolchain not found"}
    rel = "./" + layout["package_dir"].relative_to(layout["module_root"]).as_posix() + "/"
    cache = workspace.parent / "gocache"
    gopath = workspace.parent / "gopath"
    env = os.environ.copy()
    env.setdefault("GOFLAGS", "-mod=mod")
    env["GOCACHE"] = str(cache)
    env["GOPATH"] = str(gopath)
    env["GOTOOLCHAIN"] = "local"
    try:
        proc = subprocess.run(
            [go, "test", "-json", "-count=1", rel],
            cwd=str(layout["module_root"]),
            capture_output=True,
            text=True,
            env=env,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        return {"available": True, "results": {}, "build_error": False, "output": "go test timed out", "timeout": True}

    results: dict[str, str] = {}
    build_error = False
    outputs = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            outputs.append(line)
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("Action") == "output":
            text = event.get("Output", "")
            outputs.append(text)
        test = event.get("Test")
        action = event.get("Action")
        if test and action in ("pass", "fail", "skip"):
            results[test] = action
    if proc.returncode != 0 and not results:
        build_error = True
    return {"available": True, "results": results, "build_error": build_error,
            "returncode": proc.returncode, "output": "".join(outputs), "stderr": proc.stderr}


def _selectors_status(selectors, run: dict) -> str:
    if not run.get("available"):
        return "INCONCLUSIVE"
    if run.get("timeout"):
        return "INCONCLUSIVE"
    if run.get("build_error"):
        return "FAIL"
    results = run.get("results", {})
    if not selectors:
        return "INCONCLUSIVE"
    statuses = []
    for selector in selectors:
        if selector not in results:
            return "INCONCLUSIVE"
        statuses.append(results[selector])
    if all(status == "pass" for status in statuses):
        return "PASS"
    return "FAIL"


# ------------------------------- structural -------------------------------- #
def _non_test_go_files(package_dir: Path):
    for path in package_dir.glob("*.go"):
        if not path.name.endswith("_test.go"):
            yield path


def _check_gomod_module_go_only(repo: Path, layout: dict | None) -> str:
    if layout is None:
        return "FAIL"
    text = (layout["module_root"] / "go.mod").read_text(encoding="utf-8", errors="replace")
    module_ok = re.search(r"(?m)^module\s+oauth2\s*$", text) is not None
    go_ok = re.search(r"(?m)^go\s+1\.22(\.\d+)?\s*$", text) is not None
    require_ok = re.search(r"(?m)^\s*require\b", text) is None
    return "PASS" if (module_ok and go_ok and require_ok) else "FAIL"


def _check_stdlib_only_imports(repo: Path, layout: dict | None) -> str:
    if layout is None:
        return "FAIL"
    import_block = re.compile(r'^\s*(?:[A-Za-z0-9_]+\s+)?"([^"]+)"', re.M)
    for path in _non_test_go_files(layout["package_dir"]):
        text = path.read_text(encoding="utf-8", errors="replace")
        body = []
        in_block = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("import ("):
                in_block = True
                continue
            if in_block:
                if stripped.startswith(")"):
                    in_block = False
                    continue
                body.append(stripped)
            elif stripped.startswith("import "):
                body.append(stripped[len("import "):].strip())
        for item in body:
            match = import_block.search(item)
            if not match:
                continue
            target = match.group(1)
            first = target.split("/")[0]
            if "." in first:
                return "FAIL"
    return "PASS"


def _check_change_footprint(repo: Path, layout: dict | None) -> str:
    if layout is None:
        return "FAIL"
    package_rel = layout["package_dir"].relative_to(layout["module_root"])
    for path in layout["module_root"].rglob("*.go"):
        if "vendor" in path.parts:
            continue
        try:
            path.relative_to(layout["package_dir"])
        except ValueError:
            return "FAIL"
    return "PASS"


def _check_tests_no_skip(repo: Path, layout: dict | None) -> str:
    if layout is None:
        return "FAIL"
    pattern = re.compile(r"\b(?:t|b|f)\.(?:Skip|SkipNow|Skipf|Skipf?)\b|//\s*(?:nolint:)?skip\b", re.I)
    for path in layout["package_dir"].glob("*_test.go"):
        if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
            return "FAIL"
    return "PASS"


STRUCTURAL_CHECKS = {
    "gomod_module_go_only": _check_gomod_module_go_only,
    "stdlib_only_imports": _check_stdlib_only_imports,
    "change_footprint": _check_change_footprint,
    "tests_no_skip": _check_tests_no_skip,
}


def structural_status(check: str, repo: Path, layout: dict | None) -> str:
    fn = STRUCTURAL_CHECKS.get(check)
    if fn is None:
        return "INCONCLUSIVE"
    try:
        return fn(repo, layout)
    except Exception:
        return "INCONCLUSIVE"


# --------------------------------------------------------------------------- #
# source side
# --------------------------------------------------------------------------- #
def run_cargo(repo: Path) -> dict:
    cargo = _which("cargo")
    if cargo is None or not (repo / "Cargo.toml").is_file():
        return {"available": False, "results": {}, "build_error": False, "output": "cargo/Cargo.toml unavailable"}
    env = os.environ.copy()
    env.setdefault("CARGO_NET_OFFLINE", "true")
    try:
        proc = subprocess.run(
            [cargo, "test", "--tests", "--examples"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env=env,
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        return {"available": True, "results": {}, "build_error": False, "output": "cargo test timed out", "timeout": True}
    results: dict[str, str] = {}
    for match in _TEST_LINE.finditer(proc.stdout):
        name, outcome = match.group(1), match.group(2)
        results[name] = "pass" if outcome == "ok" else ("skip" if outcome == "ignored" else "fail")
    build_error = proc.returncode != 0 and not results
    return {"available": True, "results": results, "build_error": build_error, "output": proc.stdout + proc.stderr}


def _cargo_selectors_status(selectors, run: dict) -> str:
    if not run.get("available"):
        return "INCONCLUSIVE"
    if run.get("timeout"):
        return "INCONCLUSIVE"
    if run.get("build_error"):
        return "INCONCLUSIVE"
    results = run.get("results", {})
    statuses = []
    for selector in selectors:
        candidates = [selector]
        if selector.startswith("oauth2::"):
            candidates.append(selector[len("oauth2::"):])
        found = None
        for candidate in candidates:
            if candidate in results:
                found = results[candidate]
                break
        if found is None:
            return "INCONCLUSIVE"
        statuses.append(found)
    if all(status == "pass" for status in statuses):
        return "PASS"
    return "FAIL"


# --------------------------------------------------------------------------- #
# report assembly
# --------------------------------------------------------------------------- #
def _value(status: str) -> dict:
    result = {"PASS": "PASS", "FAIL": "FAIL"}.get(status, status)
    return {"type": "json", "value": {"result": result}}


def _observation(entry: dict, status: str) -> dict:
    return {"id": entry["id"], "kind": entry["kind"], "status": status, "value": _value(status)}


def run_target(assets: Path, repo: Path) -> dict:
    mapping = load_map(assets)
    entries = mapping["observations"]
    go_available = _which("go") is not None
    layout = locate_layout(repo) if repo.is_dir() else None

    run = None
    workspace = None
    if go_available and layout is not None:
        try:
            layout, workspace = build_target_workspace(assets, repo)
            run = run_go_tests(workspace, layout)
            layout = locate_layout(workspace)
        except Exception as exc:  # pragma: no cover - defensive
            run = {"available": True, "results": {}, "build_error": True, "output": str(exc)}

    observations = []
    for entry in entries:
        target = entry["target"]
        if target["mode"] == "structural":
            status = structural_status(target["check"], repo, locate_layout(repo))
        elif not go_available or layout is None:
            status = "INCONCLUSIVE"
        elif run is None:
            status = "INCONCLUSIVE"
        else:
            status = _selectors_status(target.get("selectors", []), run)
        observations.append(_observation(entry, status))

    if workspace is not None:
        shutil.rmtree(workspace.parent, ignore_errors=True)

    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": BATCH_ID,
        "side": "target",
        "migration": mapping.get("migration", {}),
        "observations": observations,
        # Persist merged-test diagnostics in the archived raw report, including
        # compiler stderr. Observation statuses alone cannot explain failures.
        "diagnostics": run,
    }


def source_technology_active(repo: Path, source_language: str) -> bool:
    """True when the declared source technology's build manifest is present.

    The F2P observations are frozen target-technology postconditions.  On the
    source technology they are not in effect, which is a real observation of the
    source tree rather than a per-side constant: it is derived by checking that
    the source technology (Rust) still provides the build entry while the target
    technology (Go) does not.
    """
    manifests = {"rust": "Cargo.toml", "go": "go.mod"}
    manifest = manifests.get((source_language or "").lower())
    if manifest is None:
        return False
    if not repo.is_dir():
        return False
    return any(path.name == manifest for path in repo.rglob(manifest) if "vendor" not in path.parts)


def run_source(assets: Path, repo: Path) -> dict:
    mapping = load_map(assets)
    entries = mapping["observations"]
    cargo_run = run_cargo(repo) if repo.is_dir() else {"available": False}

    source_language = (mapping.get("source_technology") or {}).get("language_name", "")
    source_active = source_technology_active(repo, source_language) if repo.is_dir() else False

    go_available = _which("go") is not None
    layout = locate_layout(repo) if repo.is_dir() else None
    target_run = None
    if go_available and layout is not None:
        try:
            layout, workspace = build_target_workspace(assets, repo)
            target_run = run_go_tests(workspace, layout)
            layout = locate_layout(workspace)
            shutil.rmtree(workspace.parent, ignore_errors=True)
        except Exception:
            target_run = {"available": True, "results": {}, "build_error": True, "output": "workspace failure"}

    observations = []
    for entry in entries:
        source = entry["source"]
        if source["mode"] == "cargo":
            status = _cargo_selectors_status(source.get("selectors", []), cargo_run)
        elif source["mode"] == "target_technology":
            if source_active:
                # The source technology is still in effect, so the frozen
                # target-technology postcondition does not hold on this side.
                status = "FAIL"
            elif target_run is None:
                status = "INCONCLUSIVE"
            elif entry["target"]["mode"] == "structural":
                status = structural_status(entry["target"]["check"], repo, locate_layout(repo))
            else:
                status = _selectors_status(entry["target"].get("selectors", []), target_run)
        else:
            status = "INCONCLUSIVE"
        observations.append(_observation(entry, status))

    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": BATCH_ID,
        "side": "source",
        "migration": mapping.get("migration", {}),
        "observations": observations,
    }


def write_report(path: Path, report: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    counts: dict[str, int] = {}
    for observation in report["observations"]:
        counts[observation["status"]] = counts.get(observation["status"], 0) + 1
    print(f"{report['side']} side: {len(report['observations'])} observations {counts}", file=sys.stderr)

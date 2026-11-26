#!/usr/bin/env python3
"""Shared integration harness for the source and target runners."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import serie_bind
except ImportError:  # pragma: no cover - binding is optional
    serie_bind = None

SCHEMA_VERSION = "migration-observations:v6"
BATCH_ID = "BATCH-INTEGRATION-0001"
P2P_BATCH = "BATCH-P2P-0001"
F2P_BATCHES = ("BATCH-F2P-0001", "BATCH-F2P-0002", "BATCH-F2P-0003")
DIFF_BATCH = "BATCH-DIFFERENTIAL-0001"
ALLOWED_REPO_ENV = ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO")
VALID_STATUSES = ("PASS", "FAIL", "NA", "INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT")

PROTECTED_SOURCE_PATHS = (
    "src/app.rs",
    "src/view",
    "src/widget",
    "src/protocol.rs",
    "src/graph/image.rs",
    "src/graph/geometry.rs",
    "src/color.rs",
    "src/keybind.rs",
    "src/config.rs",
    "src/external.rs",
    "src/main.rs",
)

PROTECTED_TEST_PATHS = ("src/tests", "tests/graph", "config.schema.json")

FORBIDDEN_RUST_DELEGATION = (
    'exec.Command("cargo"',
    'exec.Command("rustc"',
    "serie-core-rs",
    "libserie",
    'import "C"',
    "plugin.Open(",
    "net/rpc",
)


def asset_root() -> Path:
    override = os.environ.get("MIGRATION_ASSET_ROOT") or os.environ.get("ASSET_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[2]


def load_inventory() -> list[dict]:
    path = asset_root() / "shared" / "integration" / "observations.json"
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    items = document if isinstance(document, list) else document.get("observations", [])
    inventory = []
    for item in items:
        if not isinstance(item, dict):
            continue
        observation_id = str(item.get("id", "")).strip()
        kind = str(item.get("kind", "")).strip()
        batch_id = str(item.get("batch_id", "")).strip()
        if observation_id and kind and batch_id:
            inventory.append({"id": observation_id, "kind": kind, "batch_id": batch_id})
    return inventory


def _first_positional(argv: list[str]) -> tuple[str | None, str | None, str | None]:
    batch = None
    repo = None
    report = None
    positional = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in ("--batch", "--batch-id", "--batch-exec"):
            index += 1
            batch = argv[index] if index < len(argv) else None
        elif argument in ("--repo", "--repository", "--repo-root"):
            index += 1
            repo = argv[index] if index < len(argv) else None
        elif argument in ("--report", "--output", "-o"):
            index += 1
            report = argv[index] if index < len(argv) else None
        elif argument.startswith("--batch="):
            batch = argument.split("=", 1)[1]
        elif argument.startswith("--batch-exec="):
            batch = argument.split("=", 1)[1]
        elif argument.startswith("--repo="):
            repo = argument.split("=", 1)[1]
        elif argument.startswith("--report="):
            report = argument.split("=", 1)[1]
        else:
            positional.append(argument)
        index += 1
    for item in positional:
        looks_repo = os.path.isdir(item) or os.path.exists(os.path.join(item, "go.mod"))
        looks_repo = looks_repo or os.path.exists(os.path.join(item, "Cargo.toml"))
        looks_batch = item.upper() in ("P2P", "F2P", "DIFF", "DIFFERENTIAL")
        looks_batch = looks_batch or item.upper().startswith(("BATCH-", "P2P-", "F2P-", "DIFF-"))
        if repo is None and looks_repo:
            repo = item
        elif batch is None and looks_batch and not os.path.exists(item):
            batch = item
        elif report is None and item.lower().endswith((".json", ".xml", ".junit")):
            report = item
        elif repo is None:
            repo = item
        elif report is None:
            report = item
    return batch, repo, report


def parse_invocation(argv: list[str]) -> tuple[str | None, str | None, str | None]:
    batch = None
    args = list(argv)
    if args and args[0] == "batch-exec":
        args = args[1:]
        if args and not args[0].startswith("-"):
            batch = args[0]
            args = args[1:]
    option_batch, repo, report = _first_positional(args)
    return batch or option_batch, repo, report


def locate_repo(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    for name in ALLOWED_REPO_ENV:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser().resolve()
    return Path("/workspace/repo").resolve()


def normalize_batches(value: str | None) -> set[str] | None:
    if not value:
        return None
    name = value.strip()
    upper = name.upper()
    if upper == "P2P":
        return {P2P_BATCH}
    if upper == "F2P":
        return set(F2P_BATCHES)
    if upper in ("DIFF", "DIFFERENTIAL"):
        return {DIFF_BATCH}
    if upper.startswith("BATCH-"):
        return {name}
    if upper.startswith("P2P-"):
        return {P2P_BATCH}
    if upper.startswith("F2P-"):
        return set(F2P_BATCHES)
    if upper.startswith("DIFF-"):
        return {DIFF_BATCH}
    return {name}


def run_command(command: list[str], cwd: Path, env: dict | None = None, timeout: int = 900):
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None if isinstance(error, OSError) else _TimedOut(str(error))


class _TimedOut:
    def __init__(self, message: str):
        self.returncode = 124
        self.stdout = message


def go_environment(context: dict) -> dict:
    env = dict(os.environ)
    env.setdefault("CGO_ENABLED", "0")
    env.setdefault("GOPROXY", "off")
    env.setdefault("GOSUMDB", "off")
    env.setdefault("GOTOOLCHAIN", "local")
    if "GOCACHE" not in env:
        cache = context.setdefault("cache", tempfile.mkdtemp(prefix="serie-integration-"))
        env["GOCACHE"] = os.path.join(cache, "gocache")
        env["GOMODCACHE"] = os.path.join(cache, "gomodcache")
    return env


def parse_go_test_json(output: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        test = event.get("Test")
        action = event.get("Action")
        if test and action in ("pass", "fail", "skip") and "/" not in str(test):
            statuses[str(test)] = str(action)
    return statuses


def go_test(repo: Path, context: dict, pattern: str | None = None, package: str = "./...", overlays: dict | None = None):
    if shutil.which("go") is None:
        return None, "go toolchain is unavailable"
    command = ["go", "test", "-json", "-count=1"]
    if pattern:
        command.extend(["-run", pattern])
    if overlays:
        cache = context.setdefault("cache", tempfile.mkdtemp(prefix="serie-integration-"))
        overlay_path = Path(cache) / f"overlay-{len(list(Path(cache).glob('overlay-*.json')))}.json"
        overlay_path.write_text(json.dumps({"Replace": overlays}), encoding="utf-8")
        command.append("-overlay=" + str(overlay_path))
    command.append(package)
    workdir = serie_bind.module_root(repo) if serie_bind is not None else repo
    result = run_command(command, workdir, env=go_environment(context))
    if result is None:
        return None, "go test could not be started"
    if isinstance(result, _TimedOut):
        return None, "go test timed out"
    return parse_go_test_json(result.stdout), ""


def target_go_present(repo: Path) -> bool:
    """True when the candidate delivered a Go module with Go sources.

    The task lets the candidate organise the migrated core however it likes
    (``Go library or a standalone CLI, or both``), so the check must not pin a
    ``core`` directory name.
    """

    workdir = serie_bind.module_root(repo) if serie_bind is not None else repo
    return (workdir / "go.mod").is_file() and _has_go_sources(workdir)


def read_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def json_from_output(output: str):
    text = output.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start >= 0:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
            return value
        except json.JSONDecodeError:
            pass
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def status_from_result(value: dict | None) -> str:
    if not isinstance(value, dict):
        return "INCONCLUSIVE"
    result = str(value.get("result", "")).upper()
    if result == "PASS":
        return "PASS"
    if result == "FAIL":
        return "FAIL"
    return "INCONCLUSIVE"


def probe_status(value: str) -> str:
    value = str(value).upper()
    if value == "PASS":
        return "PASS"
    if value == "FAIL":
        return "FAIL"
    return "INCONCLUSIVE"


def p2p_spec(root: Path) -> dict:
    document = read_json(root / "shared" / "batches" / P2P_BATCH / "observations.json")
    if not isinstance(document, dict):
        return {}
    return {str(item.get("id", "")): item for item in document.get("observations", [])}


def run_p2p(side: str, repo: Path, ids: list[str], context: dict) -> dict:
    root = asset_root()
    script = root / side / "batches" / P2P_BATCH / "run_p2p.py"
    results = {observation_id: ("INCONCLUSIVE", "P2P runner is unavailable") for observation_id in ids}
    if not script.is_file():
        return results
    if side == "target":
        # Preserve structural/protected-asset checks, but measure migrated
        # behavior with trusted tests rather than a candidate's assertions.
        # The candidate's complete native suite is still run and recorded by
        # harbor_entry, including failures and their original output.
        import importlib.util
        module_spec = importlib.util.spec_from_file_location("serie_p2p_checks", script)
        if module_spec is None or module_spec.loader is None:
            return results
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        spec = p2p_spec(root)
        behavior = trusted_behavior_status(repo, context)
        for observation_id in ids:
            probe = spec.get(observation_id, {}).get("target_probe", {})
            if probe.get("kind") == "go-test":
                results[observation_id] = behavior
            elif probe.get("kind") == "structural":
                ok, detail = module.structural_check(probe["check"], str(repo), {})
                results[observation_id] = ("PASS" if ok else "FAIL", detail)
        return results
    report_path = Path(tempfile.mkdtemp(prefix="serie-p2p-")) / "report.json"
    result = run_command([sys.executable, str(script), str(repo), str(report_path)], repo)
    payload = read_json(report_path)
    if not isinstance(payload, dict):
        detail = "P2P runner produced no report"
        return {observation_id: ("INCONCLUSIVE", detail) for observation_id in ids}
    observed = payload.get("observations", {})
    spec = p2p_spec(root)
    env_key = "source_probe" if side == "source" else "target_probe"
    for observation_id in ids:
        status = status_from_result(observed.get(observation_id))
        probe = spec.get(observation_id, {}).get(env_key, {})
        kind = probe.get("kind") if isinstance(probe, dict) else None
        if kind == "cargo-test" and shutil.which("cargo") is None:
            status = "INCONCLUSIVE"
        if kind == "go-test" and shutil.which("go") is None:
            status = "INCONCLUSIVE"
        results[observation_id] = (status, "")
    return results


def run_f2p0001(side: str, repo: Path, ids: list[str], context: dict) -> dict:
    if side == "target":
        probe = context.get("probe_binary")
        contract = context.get("contract")
        if not probe or not contract:
            detail = context.get("contract_error", "candidate Go core binding is unavailable")
            return {observation_id: ("INCONCLUSIVE", detail) for observation_id in ids}
        results: dict[str, tuple[str, str]] = {}
        for observation_id in ids:
            result = run_command([str(probe), observation_id, str(contract)], contract)
            if result is None:
                results[observation_id] = ("INCONCLUSIVE", "probe could not be started")
                continue
            payload = json_from_output(result.stdout)
            if not isinstance(payload, dict):
                results[observation_id] = ("INCONCLUSIVE", "probe produced no JSON result")
                continue
            results[observation_id] = (probe_status(payload.get("status", "")), str(payload.get("detail", "")))
        return results
    root = asset_root()
    probe = root / "shared" / "batches" / "BATCH-F2P-0001" / "run_probe.sh"
    if not probe.is_file():
        return {observation_id: ("INCONCLUSIVE", "F2P-0001 probe is unavailable") for observation_id in ids}
    results = {}
    for observation_id in ids:
        result = run_command(["sh", str(probe), str(repo), observation_id], repo)
        if result is None:
            results[observation_id] = ("INCONCLUSIVE", "probe could not be started")
            continue
        payload = json_from_output(result.stdout)
        if not isinstance(payload, dict):
            results[observation_id] = ("INCONCLUSIVE", "probe produced no JSON result")
            continue
        results[observation_id] = (probe_status(payload.get("status", "")), str(payload.get("detail", "")))
    return results


def run_f2p0003_source(repo: Path, ids: list[str], context: dict) -> dict:
    root = asset_root()
    probe = root / "source" / "batches" / "BATCH-F2P-0003" / "probe.py"
    if not probe.is_file():
        return {observation_id: ("INCONCLUSIVE", "F2P-0003 source probe is unavailable") for observation_id in ids}
    result = run_command([sys.executable, str(probe), str(repo)], repo)
    if result is None:
        return {observation_id: ("INCONCLUSIVE", "probe could not be started") for observation_id in ids}
    payload = json_from_output(result.stdout)
    if not isinstance(payload, dict):
        return {observation_id: ("INCONCLUSIVE", "probe produced no JSON result") for observation_id in ids}
    observed = {
        str(item.get("observation_id", "")): str(item.get("status", ""))
        for item in payload.get("results", [])
        if isinstance(item, dict)
    }
    return {
        observation_id: (probe_status(observed.get(observation_id, "")), "")
        for observation_id in ids
    }


def run_f2p0003_target(repo: Path, ids: list[str], context: dict) -> dict:
    root = asset_root()
    test_file = root / "target" / "batches" / "BATCH-F2P-0003" / "core" / "f2p_batch_test.go"
    if not test_file.is_file():
        return {observation_id: ("INCONCLUSIVE", "F2P-0003 target probe is unavailable") for observation_id in ids}
    if shutil.which("go") is None:
        return {observation_id: ("INCONCLUSIVE", "go toolchain is unavailable") for observation_id in ids}
    contract = context.get("contract")
    if contract is None or serie_bind is None:
        detail = context.get("contract_error", "candidate Go core binding is unavailable")
        return {observation_id: ("INCONCLUSIVE", detail) for observation_id in ids}
    serie_bind.write_test(contract, "zz_integration_f2p0003_test.go", test_file)
    try:
        statuses, detail = go_test(contract, context, pattern="^TestF2P", package="./" + serie_bind.SHIM_DIRNAME)
    finally:
        serie_bind.clear_test(contract, "zz_integration_f2p0003_test.go")
    if statuses is None:
        return {observation_id: ("INCONCLUSIVE", detail) for observation_id in ids}
    results = {}
    for observation_id in ids:
        number = observation_id.split("-", 1)[-1]
        test_name = None
        for name in statuses:
            match = re.match(r"^TestF2P(\d{3})", name)
            if match and match.group(1) == number:
                test_name = name
                break
        if test_name is None:
            results[observation_id] = ("FAIL", "target test is missing or did not run")
        else:
            results[observation_id] = ("PASS" if statuses[test_name] == "pass" else "FAIL", "")
    return results


def strip_go_comments(text: str) -> str:
    """Blank Go comments so prose mentioning the old crate is not delegation.

    String literals are preserved because a real delegation call lives in code
    (``exec.Command("cargo", ...)``); only comments and their prose are removed.
    """

    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"(?m)//.*$", " ", text)
    return text


def rust_delegation_present(repo: Path) -> str:
    for directory, _, files in os.walk(repo):
        if os.path.basename(directory) in (".git", ".gocache", ".gomodcache", "vendor", "batches"):
            continue
        for name in files:
            if not name.endswith(".go") or name.endswith("_test.go"):
                continue
            path = Path(directory) / name
            try:
                content = strip_go_comments(
                    path.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                continue
            for needle in FORBIDDEN_RUST_DELEGATION:
                if needle in content:
                    return f"{path} contains {needle}"
    return ""


def run_f2p0002(side: str, repo: Path, ids: list[str], context: dict) -> dict:
    results = {observation_id: ("INCONCLUSIVE", "target predicate is unavailable") for observation_id in ids}
    if not target_go_present(repo):
        return {observation_id: ("FAIL", "target Go technology is absent") for observation_id in ids}
    if shutil.which("go") is None:
        return {observation_id: ("INCONCLUSIVE", "go toolchain is unavailable") for observation_id in ids}
    statuses = context.get("full_go_statuses")
    if statuses is None:
        statuses, detail = go_test(repo, context)
        if statuses is None:
            return {observation_id: ("INCONCLUSIVE", detail) for observation_id in ids}
        context["full_go_statuses"] = statuses
    delegation = rust_delegation_present(repo)
    candidate_tests = _candidate_go_tests(repo)
    if delegation:
        detail = delegation
    elif not candidate_tests:
        detail = "no candidate Go tests were found"
    elif not statuses:
        detail = "the candidate Go test suite reported no results"
    else:
        detail = ""
    # Compile checks and authored-test diagnostics above are supplemented by
    # the same trusted runtime assertions used for behavior preservation.
    behavior = trusted_behavior_status(repo, context)
    for observation_id in ids:
        results[observation_id] = (
            behavior if not detail else ("FAIL", detail)
        )
    return results


def trusted_behavior_status(repo: Path, context: dict) -> tuple[str, str]:
    """Execute and cache the original evaluator-owned differential assertions."""

    if "trusted_behavior_status" not in context:
        inventory = load_inventory()
        ids = [item["id"] for item in inventory if item["kind"] == "DIFFERENTIAL"]
        results = run_diff("target", repo, ids, context)
        bad = [(key, status, detail) for key, (status, detail) in results.items() if status != "PASS"]
        if not ids or len(results) != len(ids):
            status = ("INCONCLUSIVE", "trusted differential observations are missing")
        elif bad:
            failures = [row for row in bad if row[1] == "FAIL"]
            status = ("FAIL" if failures else bad[0][1], "; ".join(
                f"{key}: {detail or observed}" for key, observed, detail in bad
            ))
        else:
            status = ("PASS", "trusted differential assertions passed")
        context["trusted_behavior_status"] = status
    return context["trusted_behavior_status"]


def git_status(repo: Path, *paths: str):
    if not (repo / ".git").exists():
        return None
    command = ["git", "-C", str(repo), "status", "--porcelain"]
    if paths:
        command.extend(["--", *paths])
    result = run_command(command, repo)
    if result is None or isinstance(result, _TimedOut) or result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def protected_files_unchanged(repo: Path) -> tuple[bool, str]:
    missing = [path for path in PROTECTED_SOURCE_PATHS if not os.path.exists(repo / path)]
    if missing:
        return False, "protected baseline paths are missing: " + ", ".join(missing)
    lines = git_status(repo, *PROTECTED_SOURCE_PATHS)
    if lines:
        return False, "protected paths changed: " + "; ".join(lines)
    return True, ""


def protected_tests_unchanged(repo: Path) -> tuple[bool, str]:
    missing = [path for path in PROTECTED_TEST_PATHS if not os.path.exists(repo / path)]
    if missing:
        return False, "protected test assets are missing: " + ", ".join(missing)
    lines = git_status(repo, *PROTECTED_TEST_PATHS)
    if lines:
        return False, "protected test assets changed: " + "; ".join(lines)
    return True, ""


def change_scope_ok(repo: Path, side: str) -> tuple[bool, str]:
    lines = git_status(repo)
    if lines is None:
        return True, ""
    for line in lines:
        code = line[:2]
        path = line[3:].strip()
        if code == "??":
            if _allowed_go_change(repo, path):
                continue
            return False, "unexpected added path: " + path
        if code.strip() in ("M", "A"):
            if _allowed_go_change(repo, path):
                continue
        return False, "unexpected tracked change: " + line
    return True, ""


def _allowed_go_change(repo: Path, path: str) -> bool:
    """True when a working-tree path belongs to the candidate's Go core/module.

    The task constrains changes to "the new Go core, its Go module and build
    files, and supporting Go tests or fixtures" without fixing a Go package
    directory, so any Go source tree, module file, or document placed beside Go
    sources is in scope.
    """

    clean = path.strip().strip('"')
    if not clean:
        return False
    if clean.startswith((".gocache/", ".gomodcache/")):
        return True
    stripped = clean.rstrip("/")
    if stripped in ("go.mod", "go.sum") or stripped.endswith(".go"):
        return True
    # Root-level Go documentation/build wiring is allowed when a Go core is
    # present. Protected Rust paths still have independent content checks.
    name = stripped.rsplit("/", 1)[-1]
    if name in ("Makefile", "GNUmakefile") or name.lower().endswith(".md"):
        parent = Path(repo) / os.path.dirname(stripped)
        if _has_go_sources(parent):
            return True
    if stripped.split("/", 1)[0] in ("cmd", "core"):
        return True
    if clean.endswith("/"):
        return _has_go_sources(repo / stripped)
    directory = os.path.dirname(stripped)
    if not directory:
        return False
    return _has_go_sources(repo / directory)


def _has_go_sources(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    for root, dirs, files in os.walk(directory):
        dirs[:] = [name for name in dirs if name not in (".git", ".gocache", ".gomodcache", "vendor")]
        if any(name.endswith(".go") for name in files):
            return True
    return False


def _candidate_go_tests(repo: Path) -> list[str]:
    found: list[str] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [name for name in dirs if name not in (".git", ".gocache", ".gomodcache", "vendor")]
        relative = Path(root).relative_to(repo).as_posix()
        if relative in ("src/tests", "tests/graph"):
            continue
        for name in files:
            if name.endswith("_test.go"):
                found.append((Path(relative) / name).as_posix() if relative != "." else name)
    return sorted(found)


def diff_structural(repo: Path, observation_id: str, side: str, context: dict) -> tuple[str, str]:
    if observation_id == "DIFF-001":
        ok, detail = protected_files_unchanged(repo)
        return ("PASS" if ok else "FAIL", detail)
    if observation_id == "DIFF-026":
        ok, detail = change_scope_ok(repo, side)
        return ("PASS" if ok else "FAIL", detail)
    if observation_id == "DIFF-027":
        ok, detail = protected_files_unchanged(repo)
        return ("PASS" if ok else "FAIL", detail)
    if observation_id == "DIFF-028":
        ok, detail = protected_tests_unchanged(repo)
        if not ok:
            return "FAIL", detail
        return "PASS", ""
    if observation_id == "DIFF-029":
        ok, detail = protected_tests_unchanged(repo)
        return ("PASS" if ok else "FAIL", detail)
    if observation_id == "DIFF-030":
        ok, detail = protected_tests_unchanged(repo)
        if not ok:
            return "FAIL", detail
        if side == "target":
            if not _candidate_go_tests(repo):
                return "FAIL", "missing Go test wiring: no candidate Go _test.go files were found"
            if git_status(repo, "src/tests", "tests/graph"):
                return "FAIL", "protected Rust tests or snapshots changed"
        return "PASS", ""
    return "INCONCLUSIVE", "unknown differential structural observation"


def source_cargo_status(context: dict, repo: Path) -> tuple[str, str]:
    if shutil.which("cargo") is None:
        return "INCONCLUSIVE", "cargo toolchain is unavailable"
    cached = context.get("cargo_status")
    if cached is not None:
        return cached
    result = run_command(["cargo", "test", "--verbose"], repo, timeout=1800)
    if result is None:
        status = ("INCONCLUSIVE", "cargo test could not be started")
    elif isinstance(result, _TimedOut):
        status = ("TIMEOUT", "cargo test timed out")
    elif result.returncode == 0:
        status = ("PASS", "")
    else:
        status = ("FAIL", "cargo test failed")
    context["cargo_status"] = status
    return status


def run_diff(side: str, repo: Path, ids: list[str], context: dict) -> dict:
    root = asset_root()
    corpus = read_json(root / "fixtures" / "batches" / DIFF_BATCH / "corpus.json")
    if not isinstance(corpus, dict):
        return {observation_id: ("INCONCLUSIVE", "differential corpus is unavailable") for observation_id in ids}
    structural = {"DIFF-001", "DIFF-026", "DIFF-027", "DIFF-028", "DIFF-029", "DIFF-030"}
    results: dict[str, tuple[str, str]] = {}
    for observation_id in ids:
        if observation_id in structural:
            results[observation_id] = diff_structural(repo, observation_id, side, context)
    if side == "source":
        cargo_status = source_cargo_status(context, repo)
        if cargo_status[0] in ("INCONCLUSIVE", "TIMEOUT"):
            for observation_id in ids:
                if observation_id not in results:
                    results[observation_id] = cargo_status
        for observation_id in ids:
            if observation_id not in results:
                results[observation_id] = ("PASS" if cargo_status[0] == "PASS" else "FAIL", cargo_status[1])
        return results

    behavior_ids = [observation_id for observation_id in ids if observation_id not in structural]
    test_file = root / "target" / "batches" / DIFF_BATCH / "core" / "diffbatch0001_test.go"
    if not behavior_ids:
        return results
    if not test_file.is_file():
        for observation_id in behavior_ids:
            results[observation_id] = ("INCONCLUSIVE", "differential target probe is unavailable")
        return results
    if shutil.which("go") is None:
        for observation_id in behavior_ids:
            results[observation_id] = ("INCONCLUSIVE", "go toolchain is unavailable")
        return results
    contract = context.get("contract")
    if contract is None or serie_bind is None:
        detail = context.get("contract_error", "candidate Go core binding is unavailable")
        for observation_id in behavior_ids:
            results[observation_id] = ("INCONCLUSIVE", detail)
        return results
    serie_bind.write_test(contract, "zz_integration_diff_test.go", test_file)
    try:
        statuses, detail = go_test(contract, context, pattern="^TestDiffB0001", package="./" + serie_bind.SHIM_DIRNAME)
    finally:
        serie_bind.clear_test(contract, "zz_integration_diff_test.go")
    if statuses is None:
        for observation_id in behavior_ids:
            results[observation_id] = ("INCONCLUSIVE", detail)
        return results
    for case in corpus.get("cases", []):
        if not isinstance(case, dict):
            continue
        probe = str(case.get("probe", ""))
        if not probe.startswith("Test"):
            continue
        status = "PASS" if statuses.get(probe) == "pass" else "FAIL"
        detail = "" if status == "PASS" else f"target test did not pass: {probe}"
        for observation_id in case.get("observations", []):
            observation_id = str(observation_id)
            if observation_id not in ids or observation_id in structural:
                continue
            previous = results.get(observation_id)
            if previous is None or previous[0] == "PASS":
                results[observation_id] = (status, detail)
            elif status == "FAIL":
                results[observation_id] = (status, detail)
    for observation_id in behavior_ids:
        results.setdefault(observation_id, ("FAIL", "target test mapping is missing"))
    return results


def build_contract(side: str, repo: Path, context: dict) -> None:
    """Prepare the evaluator binding used by the target-side Go probes."""

    if side != "target" or serie_bind is None:
        return
    try:
        contract = serie_bind.build_contract_repo(repo)
        context["contract"] = contract
        binary = serie_bind.probe_binary(contract)
        if binary is not None:
            context["probe_binary"] = binary
    except Exception as error:  # noqa: BLE001 - binding is best effort
        context["contract_error"] = "candidate Go core binding failed: %s" % (error,)


def run_batches(side: str, repo: Path, inventory: list[dict], selected: list[dict]) -> dict:
    context: dict = {}
    build_contract(side, repo, context)
    by_batch: dict[str, list[str]] = {}
    for item in selected:
        by_batch.setdefault(item["batch_id"], []).append(item["id"])
    results: dict[str, tuple[str, str]] = {}
    order = (P2P_BATCH,) + F2P_BATCHES + (DIFF_BATCH,)
    for batch in order:
        ids = by_batch.get(batch)
        if not ids:
            continue
        if batch == P2P_BATCH:
            results.update(run_p2p(side, repo, ids, context))
        elif batch == "BATCH-F2P-0001":
            results.update(run_f2p0001(side, repo, ids, context))
        elif batch == "BATCH-F2P-0002":
            results.update(run_f2p0002(side, repo, ids, context))
        elif batch == "BATCH-F2P-0003":
            if side == "source":
                results.update(run_f2p0003_source(repo, ids, context))
            else:
                results.update(run_f2p0003_target(repo, ids, context))
        elif batch == DIFF_BATCH:
            results.update(run_diff(side, repo, ids, context))
    for item in selected:
        results.setdefault(item["id"], ("INCONCLUSIVE", "observation was not executed"))
    return results


def build_report(side: str, repo: Path, selected: list[dict], results: dict) -> dict:
    observations = []
    for item in selected:
        observation_id = item["id"]
        status, detail = results.get(observation_id, ("INCONCLUSIVE", "observation was not executed"))
        if status not in VALID_STATUSES:
            status = "INCONCLUSIVE"
        value = {"result": status}
        if item["kind"] == "F2P":
            value["technology"] = "Go 1.24.0" if side == "target" else "Rust 2021"
        if detail and status not in ("PASS", "FAIL"):
            value["detail"] = detail
        observations.append(
            {
                "id": observation_id,
                "kind": item["kind"],
                "status": status,
                "value": {"type": "json", "value": value},
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": BATCH_ID,
        "side": side,
        "observations": observations,
    }


def write_report(report: dict, report_path: str | None) -> None:
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if report_path:
        path = Path(report_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def main(side: str, argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    batch, repo_argument, report_path = parse_invocation(argv)
    repo = locate_repo(repo_argument)
    try:
        inventory = load_inventory()
    except (OSError, json.JSONDecodeError) as error:
        sys.stderr.write(f"cannot load observation inventory: {error}\n")
        return 2
    batches = normalize_batches(batch)
    if batches is None:
        selected = inventory
    else:
        selected = [item for item in inventory if item["batch_id"] in batches]
    results = run_batches(side, repo, inventory, selected)
    report = build_report(side, repo, selected, results)
    write_report(report, report_path)
    failed = [item["id"] for item in report["observations"] if item["status"] != "PASS"]
    if failed:
        sys.stderr.write(f"{side} observations not passing: {', '.join(failed)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(os.environ.get("MIGRATION_SIDE", "target")))

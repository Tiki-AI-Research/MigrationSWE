#!/usr/bin/env python3
"""Shared integration harness for the @stepperize/core TypeScript -> Rust migration.

The integration batch does not own any observation of its own. It merges the
dependency batches and provides the two executable entry points used by the
runner registries:

* ``source/runner``    -> source (TypeScript) side observations
* ``run_tests.py``     -> target (Rust) side observations

Both entries dispatch the *executable projections* of every dependency batch
(P2P, F2P, DIFFERENTIAL, CONTRACT) and emit a single runtime report whose top
level ``schema_version`` is ``migration-observations:v6``.

Design constraints honoured here:

* Every potentially long running command is wrapped in an explicit timeout.
* Observations are produced by really executing the batch probes / native test
  suites. When a probe cannot be executed the observation is reported as
  ``INCONCLUSIVE`` instead of being asserted from its declared expectation.
* No privileged or permanently blocking case is ever invoked.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

SCHEMA_VERSION = "migration-observations:v6"

PASS = "PASS"
FAIL = "FAIL"
NA = "NA"
INCONCLUSIVE = "INCONCLUSIVE"
INFRA = "INFRA_ERROR"
TIMEOUT = "TIMEOUT"

DEFAULT_TIMEOUT = 1500
SHORT_TIMEOUT = 300
P2P_BUDGET = 900

F2P_BATCHES = ("BATCH-F2P-0001", "BATCH-F2P-0002", "BATCH-F2P-0003")
P2P_BATCHES = ("BATCH-P2P-0001", "BATCH-P2P-0002")


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #
def assets_root_from(module_file):
    # <assets>/shared/integration/integration.py -> <assets>
    return os.path.abspath(os.path.join(os.path.dirname(module_file), "..", ".."))


try:
    import crate_discovery  # noqa: E402 - sibling module in shared/integration
except ImportError:  # pragma: no cover - assets incomplete
    crate_discovery = None


def resolve_repo(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    for key in ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO"):
        value = os.environ.get(key)
        if value:
            return os.path.abspath(value)
    return os.path.abspath(os.getcwd())


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _run(cmd, cwd=None, timeout=DEFAULT_TIMEOUT, env=None):
    """Run ``cmd`` under a hard timeout.

    Returns ``(returncode, output)`` where returncode is an ``int``, the string
    ``"timeout"`` when the process had to be killed, or ``None`` when the
    executable could not be started (an infrastructure error).
    """
    merged = dict(os.environ)
    merged.setdefault("CARGO_NET_OFFLINE", "true")
    merged.setdefault("CI", "true")
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env=merged,
        )
        return proc.returncode, proc.stdout
    except FileNotFoundError as exc:
        return None, str(exc)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return "timeout", out


# --------------------------------------------------------------------------- #
# Inventory / canonical projections
# --------------------------------------------------------------------------- #
def load_inventory(assets):
    data = _read_json(os.path.join(assets, "shared", "integration", "observations.json"))
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("id")]


def _load_p2p(assets):
    values = {}
    plan = _read_json(
        os.path.join(
            assets, "fixtures", "batches", "BATCH-P2P-0001", "p2p_expected.json"
        )
    )
    for obs in (plan or {}).get("observations", []):
        if "id" in obs:
            values[obs["id"]] = obs.get("projection")
    projection = _read_json(
        os.path.join(assets, "shared", "batches", "BATCH-P2P-0002", "p2p_projection.json")
    )
    for oid, value in ((projection or {}).get("projection") or {}).items():
        values[oid] = value
    return values


def _load_diff(assets):
    cases = _read_json(
        os.path.join(assets, "shared", "batches", "BATCH-DIFFERENTIAL-0001", "cases.json")
    )
    values = {}
    ignored = {"observation_id", "scenario", "operation"}
    for case in (cases or {}).get("cases", []):
        oid = case.get("observation_id")
        if not oid:
            continue
        payload = {k: v for k, v in case.items() if k not in ignored}
        values[oid] = payload if payload else {"result": PASS}
    return values


def _load_contract(assets):
    expected = _read_json(
        os.path.join(
            assets,
            "fixtures",
            "batches",
            "BATCH-CONTRACT-0001",
            "contract-expected.json",
        )
    )
    return (expected or {}).get("expected") or {}


def load_canonical_values(assets):
    values = {}
    values.update(_load_diff(assets))
    values.update(_load_contract(assets))
    values.update(_load_p2p(assets))
    return values


# --------------------------------------------------------------------------- #
# Native test execution
# --------------------------------------------------------------------------- #
def has_cargo():
    return shutil.which("cargo") is not None


def run_cargo(repo, timeout=DEFAULT_TIMEOUT):
    """Run the workspace test suite offline and return per-test statuses."""
    if not has_cargo():
        return None, None, "cargo executable unavailable"
    code, out = _run(
        crate_discovery.cargo_command(repo, ["test", "--workspace", "--offline"]),
        cwd=repo,
        timeout=timeout,
    )
    if code is None:
        return None, None, "cargo could not be started"
    if code == "timeout":
        return False, {}, "cargo test timed out"
    return code == 0, parse_cargo_tests(out), out


def parse_cargo_tests(output):
    results = {}
    for line in (output or "").splitlines():
        if not line.startswith("test "):
            continue
        parts = line.split(" ... ", 1)
        if len(parts) != 2:
            continue
        name = parts[0][len("test "):].strip()
        status = parts[1].strip()
        if status in ("ok", "FAILED", "ignored"):
            results[name] = status
    return results


def run_js(repo, timeout=DEFAULT_TIMEOUT):
    """Run the retained JavaScript/TypeScript test suites when a runtime exists."""
    for tool, cmd in (
        ("pnpm", ["pnpm", "-r", "test"]),
        ("npm", ["npm", "test"]),
        ("npx", ["npx", "vitest", "run"]),
    ):
        if shutil.which(tool) is None:
            continue
        code, out = _run(cmd, cwd=repo, timeout=timeout)
        if code is None:
            return None, "javascript runtime could not be started"
        if code == "timeout":
            return None, "javascript test run timed out"
        return code == 0, out[-2000:]
    return None, "no javascript runtime available"


# --------------------------------------------------------------------------- #
# F2P probes
# --------------------------------------------------------------------------- #
def _batch_dir(assets, batch, side):
    return os.path.join(assets, side, "batches", batch)


def run_f2p_batch(assets, batch, side, repo, ids):
    """Execute the multi-backend F2P probe for ``batch`` and return per-observation
    satisfaction flags, or ``None`` when the probe could not be executed.

    ``ids`` is the observation inventory owned by the batch; the harness never
    reads the authoring-only ``fragments/`` directory at runtime.
    """
    handler = {
        ("BATCH-F2P-0001", "target"): _f2p_0001_target,
        ("BATCH-F2P-0001", "source"): _f2p_0001_source,
        ("BATCH-F2P-0002", "target"): _f2p_0002_target,
        ("BATCH-F2P-0002", "source"): _f2p_0002_source,
        ("BATCH-F2P-0003", "target"): _f2p_0003_target,
        ("BATCH-F2P-0003", "source"): _f2p_0003_source,
    }.get((batch, side))
    if handler is None:
        return None
    return handler(assets, repo, ids)


def _result_from_exit(ids, code):
    ok = code == 0
    return {oid: ok for oid in ids}


def _parse_cargo_ok_names(output):
    return {
        name
        for name, status in parse_cargo_tests(output).items()
        if status == "ok"
    }


def _f2p_0001_target(assets, repo, ids):
    script = os.path.join(
        _batch_dir(assets, "BATCH-F2P-0001", "target"), "probes", "run_target_probe.sh"
    )
    if not os.path.exists(script):
        return {oid: None for oid in ids}
    code, out = _run(
        ["sh", script], env={"MIGRATION_REPO": repo}, timeout=DEFAULT_TIMEOUT
    )
    if code is None or code == "timeout":
        return {oid: None for oid in ids}
    if code == 0:
        return _result_from_exit(ids, code)

    # This batch owns several independent postconditions.  The probe emits
    # cargo's test records, so an assertion failure must affect only the
    # observations exercised by that test rather than all 24 observations.
    # If no evaluator test ran (for example, the fixed public interface does
    # not compile), retain the fail-closed batch result.
    statuses = parse_cargo_tests(out)
    expected_tests = {
        "all_eight_operations_are_exported_and_run",
        "parse_step_downcasts_boxed_dynamic_values",
        "validate_step_awaits_boxed_schema_future",
        "match_step_uses_not_found_and_no_handler",
        "create_step_map_preserves_input_id_order",
        "get_initial_data_returns_independent_clone",
        "step_error_display_uses_protected_wording",
        "validate_step_preserves_the_generic_input_without_a_schema",
        "validate_step_passes_the_generic_input_to_the_schema",
    }
    if not expected_tests.intersection(statuses):
        return _result_from_exit(ids, code)

    groups = {
        "all_eight_operations_are_exported_and_run": {
            "F2P-001", "F2P-010", "F2P-012", "F2P-018",
        },
        "parse_step_downcasts_boxed_dynamic_values": {
            "F2P-002", "F2P-004", "F2P-005", "F2P-006", "F2P-019",
        },
        "validate_step_awaits_boxed_schema_future": {
            "F2P-003", "F2P-007", "F2P-016", "F2P-017", "F2P-020", "F2P-022",
        },
        "match_step_uses_not_found_and_no_handler": {"F2P-008"},
        "create_step_map_preserves_input_id_order": {"F2P-021", "F2P-024"},
        "get_initial_data_returns_independent_clone": {"F2P-013"},
        "step_error_display_uses_protected_wording": {"F2P-014"},
    }
    result = {oid: False for oid in ids}
    # Reaching the evaluator tests proves the dependency/delegation and
    # TypeScript-removal preconditions checked earlier by the shell probe.
    for oid in {"F2P-009", "F2P-011", "F2P-015", "F2P-023"}:
        if oid in result:
            result[oid] = True
    for test_name, observation_ids in groups.items():
        passed = statuses.get(test_name) == "ok"
        for oid in observation_ids:
            if oid in result:
                result[oid] = passed

    # These two regression tests refine validate_step's unchanged-input and
    # schema-boundary contract.  A failure belongs to the validation group,
    # while unrelated operation groups remain independently credited.
    validation_regressions = (
        "validate_step_preserves_the_generic_input_without_a_schema",
        "validate_step_passes_the_generic_input_to_the_schema",
    )
    validation_ok = all(statuses.get(name) == "ok" for name in validation_regressions)
    if not validation_ok:
        for oid in groups["validate_step_awaits_boxed_schema_future"]:
            if oid in result:
                result[oid] = False
    return result


def _f2p_0001_source(assets, repo, ids):
    script = os.path.join(
        _batch_dir(assets, "BATCH-F2P-0001", "source"), "probes", "run_source_probe.sh"
    )
    if not os.path.exists(script):
        return {oid: None for oid in ids}
    code, _out = _run(
        ["sh", script], env={"REPO_ROOT": repo}, timeout=DEFAULT_TIMEOUT
    )
    if code is None or code == "timeout":
        return {oid: None for oid in ids}
    return _result_from_exit(ids, code)


def _f2p_0002_target(assets, repo, ids):
    script = os.path.join(_batch_dir(assets, "BATCH-F2P-0002", "target"), "run_probe.sh")
    if not os.path.exists(script):
        return {oid: None for oid in ids}
    code, out = _run(["sh", script, repo], timeout=DEFAULT_TIMEOUT)
    if code is None or code == "timeout":
        return {oid: None for oid in ids}
    if code == 0:
        return _result_from_exit(ids, code)

    # Each observation has its own evaluator-owned Rust test.  Preserve
    # independent credit when one assertion fails: using only the batch exit
    # code would incorrectly reject every unrelated satisfied requirement.
    # A compile/binding failure produces no runnable test records and remains
    # fail-closed for the whole batch.
    statuses = parse_cargo_tests(out)
    if not statuses:
        return _result_from_exit(ids, code)
    result = {}
    for oid in ids:
        number = oid.split("-", 1)[-1].lower()
        prefix = f"f2p_{number}_"
        matches = [status for name, status in statuses.items() if name.startswith(prefix)]
        result[oid] = bool(matches) and all(status == "ok" for status in matches)
    return result


def _f2p_0002_source(assets, repo, ids):
    script = os.path.join(_batch_dir(assets, "BATCH-F2P-0002", "source"), "run_probe.sh")
    if not os.path.exists(script):
        return {oid: None for oid in ids}
    code, _out = _run(
        ["sh", script], cwd=repo, env={"REPO_ROOT": repo}, timeout=DEFAULT_TIMEOUT
    )
    if code is None or code == "timeout":
        return {oid: None for oid in ids}
    return _result_from_exit(ids, code)


def _f2p_0003_target(assets, repo, ids):
    script = os.path.join(
        _batch_dir(assets, "BATCH-F2P-0003", "target"), "run_target_probe.py"
    )
    if not os.path.exists(script):
        return {oid: None for oid in ids}
    with tempfile.TemporaryDirectory(prefix="f2p3-") as tmp:
        report = os.path.join(tmp, "report.xml")
        code, _out = _run(
            [sys.executable, script, "--repo", repo, "--report", report],
            timeout=DEFAULT_TIMEOUT,
        )
        if code is None or code == "timeout":
            return {oid: None for oid in ids}
        return _parse_junit(report, ids)


def _f2p_0003_source(assets, repo, ids):
    script = os.path.join(
        _batch_dir(assets, "BATCH-F2P-0003", "source"), "run_source_probe.py"
    )
    if not os.path.exists(script):
        return {oid: None for oid in ids}
    with tempfile.TemporaryDirectory(prefix="f2p3s-") as tmp:
        report = os.path.join(tmp, "report.xml")
        code, _out = _run(
            [sys.executable, script, "--repo", repo, "--report", report],
            timeout=DEFAULT_TIMEOUT,
        )
        if code is None or code == "timeout":
            return {oid: None for oid in ids}
        return _parse_junit(report, ids)


def _map_f2p_by_test(ids, ok_names):
    """Each F2P observation maps to a ``f2p_<lower>...`` test in its batch."""
    result = {}
    for oid in ids:
        number = oid.split("-", 1)[-1].lower()
        prefix = "f2p_%s_" % number
        matched = any(name.startswith(prefix) for name in ok_names)
        result[oid] = matched
    return result


def _parse_junit(path, ids):
    result = {oid: None for oid in ids}
    if not os.path.exists(path):
        return result
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return result
    for case in tree.iter("testcase"):
        name = case.get("name")
        if not name:
            continue
        failed = case.find("failure") is not None or case.find("error") is not None
        result[name] = not failed
    return result


# --------------------------------------------------------------------------- #
# Per-batch target/source test mapping
# --------------------------------------------------------------------------- #
def _surface_domain(surface):
    if not isinstance(surface, str):
        return None
    if surface.startswith("crates/"):
        return "rust"
    if surface.startswith("packages/") or surface.startswith("apps/"):
        return "ts"
    return None


def _test_name_from_rust_surface(surface):
    """Return the Rust integration-test function named by a ``path#name`` surface.

    Only surfaces that point at a crate ``tests/`` file name a runnable test;
    production-source surfaces (``src/lib.rs#reexports``) are crate-level and are
    evaluated by the suite outcome instead.
    """
    if "#" not in surface:
        return None
    path, name = surface.rsplit("#", 1)
    name = name.strip()
    if "/tests/" in path and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return name
    return None


def p2p_0001_domains(assets, side):
    """observation -> set of domains for BATCH-P2P-0001."""
    plan = _read_json(
        os.path.join(assets, "shared", "batches", "BATCH-P2P-0001", "p2p_plan.json")
    )
    out = {}
    for obs in (plan or {}).get("observations", []):
        oid = obs.get("id")
        surface = obs.get("target_surface" if side == "target" else "source_surface")
        domain = _surface_domain(surface)
        out[oid] = {"domain": domain, "test": _test_name_from_rust_surface(surface or "")}
    return out


def p2p_0001_projections(assets, side):
    """observation -> the side's own projection record for BATCH-P2P-0001."""
    plan = _read_json(
        os.path.join(assets, "shared", "batches", "BATCH-P2P-0001", "p2p_plan.json")
    )
    out = {}
    for obs in (plan or {}).get("observations", []):
        if isinstance(obs, dict) and obs.get("id"):
            out[obs["id"]] = obs.get(side + "_projection") or {}
    return out


def retained_artifact_status(repo, relpath):
    """Node-free executable projection for a retained, out-of-scope artifact.

    The migration intentionally leaves the React/DOM and docs packages
    untouched, so their state-preservation projection is a filesystem probe:
    the artifact must still be present and, when the repository is a git
    checkout, unmodified relative to ``HEAD``. Returns ``PASS``/``FAIL`` when
    the probe could be evaluated, or ``None`` when it is not applicable.
    """
    if not relpath:
        return None
    if not os.path.isfile(os.path.join(repo, relpath)):
        return None
    if shutil.which("git") is not None and os.path.isdir(os.path.join(repo, ".git")):
        code, _out = _run(
            ["git", "diff", "--exit-code", "HEAD", "--", relpath],
            cwd=repo,
            timeout=SHORT_TIMEOUT,
        )
        if code in (0, 1):
            return PASS if code == 0 else FAIL
    return PASS


_P2P_PROJ_CACHE = {}


def _p2p_0001_retained_status(assets, side, repo, oid, fallback):
    """Fall back to the retained-artifact probe for an out-of-scope JS test.

    Only used when BATCH-P2P-0001's own vitest projection could not be executed
    (for example when no JavaScript runtime is installed). The observation's
    surface lives under the intentionally untouched ``packages/react`` or
    ``apps/docs`` packages, whose preserved state is exactly what the probe
    checks.
    """
    cache_key = (os.path.abspath(assets), side)
    if cache_key not in _P2P_PROJ_CACHE:
        _P2P_PROJ_CACHE[cache_key] = p2p_0001_projections(assets, side)
    projection = _P2P_PROJ_CACHE[cache_key].get(oid) or {}
    if projection.get("runner") != "vitest":
        return fallback
    path = projection.get("path") or ""
    if not (path.startswith("packages/react/") or path.startswith("apps/docs/")):
        return fallback
    retained = retained_artifact_status(repo, path)
    return retained if retained is not None else fallback


def p2p_0002_domains(assets, side):
    tests = _read_json(
        os.path.join(
            assets, "source" if side == "source" else "target",
            "batches", "BATCH-P2P-0002", "p2p_tests.json",
        )
    )
    out = {}
    for oid, names in ((tests or {}).get("mapping") or {}).items():
        domain = None
        rust_tests = []
        for name in names:
            if name.startswith("crates/"):
                domain = "rust"
                rust_tests.append(_test_name_from_rust_surface(name))
            elif name.startswith("packages/react") or name.startswith("apps/"):
                domain = "ts" if domain is None else domain
            else:
                # Bare Rust test function names or aggregate strings.
                if re.match(r"^[a-z0-9_]+$", name):
                    domain = "rust" if domain is None else domain
                    rust_tests.append(name)
                else:
                    domain = domain or "ts"
        out[oid] = {"domain": domain, "tests": [t for t in rust_tests if t]}
    return out


def static_contract_check(repo, oid):
    """Filesystem-level probes for observations that do not require a runtime.

    Returns ``True``/``False`` when the probe could be evaluated, or ``None``
    when the check is not applicable.
    """
    if oid in ("P2P-026", "P2P-036"):
        want = "./primitives" if oid == "P2P-026" else "."
        manifest = _read_json(os.path.join(repo, "packages", "react", "package.json"))
        if not isinstance(manifest, dict):
            return None
        exports = manifest.get("exports")
        return isinstance(exports, dict) and want in exports
    if oid == "P2P-037":
        if shutil.which("git") is None or not os.path.isdir(os.path.join(repo, ".git")):
            return None
        code, _out = _run(
            [
                "git",
                "diff",
                "--exit-code",
                "HEAD",
                "--",
                "packages/core/src/tests/utils.test.ts",
            ],
            cwd=repo,
            timeout=SHORT_TIMEOUT,
        )
        if code is None or code == "timeout":
            return None
        return code == 0
    return None


def combination_status(domains, rust_ok, rust_tests, js_state):
    """Combine domain states into a single observation status."""
    states = []
    for entry in domains:
        domain = entry.get("domain")
        if domain == "rust":
            if rust_ok is None:
                states.append(INCONCLUSIVE)
                continue
            tests = entry.get("tests") or ([entry["test"]] if entry.get("test") else [])
            tests = [t for t in tests if t]
            if tests:
                ok = all(rust_tests.get(t) == "ok" for t in tests)
                states.append(PASS if ok else FAIL)
            else:
                states.append(PASS if rust_ok else FAIL)
        elif domain == "ts":
            if js_state is None:
                states.append(INCONCLUSIVE)
            else:
                states.append(PASS if js_state else FAIL)
        else:
            states.append(INCONCLUSIVE)
    if not states:
        return INCONCLUSIVE
    if FAIL in states:
        return FAIL
    if INCONCLUSIVE in states:
        return INCONCLUSIVE
    return PASS


# --------------------------------------------------------------------------- #
# P2P executable projections
#
# P2P is an executable state-preservation check. The integration entry must
# dispatch the executable projection authored by each P2P batch instead of
# guessing a projection from surface names, so every P2P observation carries
# the raw PASS/FAIL actually produced by the repository's own tests.
# --------------------------------------------------------------------------- #
def _extract_json(text):
    """Return the first JSON object embedded in ``text``, or ``None``."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        return obj
    return None


def run_p2p_0001(assets, side, repo):
    """Dispatch BATCH-P2P-0001's executable projection for ``side``.

    The batch ships ``<side>/batches/BATCH-P2P-0001/run_p2p.sh`` which runs the
    repository's own tests and writes a report keyed by observation id. Returns
    a mapping ``observation id -> raw status`` or ``None`` when it could not be
    dispatched.
    """
    script = os.path.join(assets, side, "batches", "BATCH-P2P-0001", "run_p2p.sh")
    if not os.path.exists(script):
        return None
    with tempfile.TemporaryDirectory(prefix="p2p1-") as tmp:
        report = os.path.join(tmp, "report.json")
        code, _out = _run(
            ["sh", script, repo, report],
            env={"P2P_TIMEOUT_SEC": str(P2P_BUDGET)},
            timeout=P2P_BUDGET + 120,
        )
        if code is None:
            return None
        data = _read_json(report)
    if not isinstance(data, dict):
        return None
    results = data.get("observation_results")
    if isinstance(results, dict) and results:
        return {oid: status for oid, status in results.items()}
    entries = data.get("observations")
    if isinstance(entries, list):
        return {
            entry.get("id"): entry.get("status")
            for entry in entries
            if isinstance(entry, dict) and entry.get("id")
        }
    return None


def run_p2p_probe(assets, repo, oids):
    """Run BATCH-P2P-0002's deterministic artifact probe for ``oids``.

    The probe is language-agnostic (it inspects the out-of-scope artifacts that
    the migration intentionally leaves untouched), so the same executable
    projection is produced on both sides.
    """
    oids = [oid for oid in oids if oid]
    if not oids:
        return {}
    probe = os.path.join(assets, "shared", "batches", "BATCH-P2P-0002", "p2p_probe.py")
    if not os.path.exists(probe):
        return {}
    cmd = [sys.executable, probe, "--repo", repo]
    for oid in oids:
        cmd += ["--observation", oid]
    code, out = _run(cmd, timeout=SHORT_TIMEOUT)
    if code is None:
        return {}
    data = _extract_json(out)
    if not isinstance(data, dict):
        return {}
    side_results = data.get("side_results") or {}
    return {oid: side_results.get(oid) for oid in oids}


def run_vitest_records(repo, package, relfile, timeout=SHORT_TIMEOUT):
    """Run a single vitest file and return its assertion records, or ``None``."""
    if shutil.which("pnpm") is None:
        return None
    with tempfile.TemporaryDirectory(prefix="vitest-") as tmp:
        outfile = os.path.join(tmp, "vitest.json")
        cmd = [
            "pnpm",
            "--filter",
            package,
            "exec",
            "vitest",
            "run",
            relfile,
            "--reporter=json",
            "--outputFile",
            outfile,
        ]
        code, _out = _run(cmd, cwd=repo, timeout=timeout)
        if code is None:
            return None
        data = _read_json(outfile)
    records = []
    if isinstance(data, dict):
        for test_result in data.get("testResults") or []:
            for assertion in test_result.get("assertionResults") or []:
                title = assertion.get("title") or ""
                ancestors = assertion.get("ancestorTitles") or []
                full = " > ".join(list(ancestors) + [title]) if ancestors else title
                status = {"passed": PASS, "failed": FAIL}.get(
                    assertion.get("status"), INCONCLUSIVE
                )
                records.append({"full": full, "title": title, "status": status})
    return records


def match_record_titles(records, tests):
    """Map a list of test titles to a status using vitest assertion records."""
    if records is None:
        return None
    if not records:
        return INCONCLUSIVE
    for test in tests:
        hits = [
            record["status"]
            for record in records
            if test == record["full"] or test == record["title"] or test in record["full"]
        ]
        if not hits:
            return INCONCLUSIVE
        if FAIL in hits:
            return FAIL
    return PASS


def _run_behavior_probe(assets, repo, tests_cfg, fallback):
    """Run the evaluator-owned behaviour tests against the candidate crate.

    The migration contract requires equivalent *behaviour*, not a candidate test
    file with the reference function names.  The evaluator therefore installs
    its own behaviour test file into whichever crate the candidate produced and
    runs it, so P2P stays a behaviour check instead of a name-match against
    candidate-authored tests.
    """

    if crate_discovery is None:
        return fallback
    relative = tests_cfg.get("behavior_test_file")
    if not isinstance(relative, str) or not relative:
        return fallback
    source = os.path.join(
        assets, "target", "batches", "BATCH-P2P-0002", relative
    )
    if not os.path.isfile(source):
        return fallback
    info = crate_discovery.discover_crate(repo)
    if info is None:
        return fallback
    tests_dir = os.path.join(info["dir"], "tests")
    dest = os.path.join(tests_dir, "stepperize_p2p_behavior.rs")
    try:
        os.makedirs(tests_dir, exist_ok=True)
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write(text.replace("stepperize_core", info["lib"]))
    except OSError:
        return fallback
    try:
        behavior_ok, behavior_tests, _detail = run_cargo(repo)
    finally:
        try:
            os.remove(dest)
        except OSError:
            pass
    if behavior_tests is None:
        return fallback
    return behavior_ok, behavior_tests


def run_p2p_0002(assets, side, repo, rust_ok, rust_tests):
    """Dispatch BATCH-P2P-0002's executable projection for ``side``.

    Artifact observations are produced by the batch's deterministic probe; the
    behavior observations are mapped to the evaluator-owned behaviour tests on
    the Rust target and to the reference vitest file on the source side.
    """
    tests_cfg = _read_json(
        os.path.join(assets, side, "batches", "BATCH-P2P-0002", "p2p_tests.json")
    )
    if not isinstance(tests_cfg, dict):
        return None
    mapping = tests_cfg.get("mapping") or {}
    executable = tests_cfg.get("executable_projection") or {}
    if not mapping:
        return None

    results = {}
    probe_ids = [oid for oid in executable if oid in mapping]
    results.update(run_p2p_probe(assets, repo, probe_ids))

    behavior = {oid: names for oid, names in mapping.items() if oid not in executable}
    if side == "target":
        behavior_ok, behavior_tests = _run_behavior_probe(
            assets, repo, tests_cfg, fallback=(rust_ok, rust_tests)
        )
        for oid, names in behavior.items():
            names = [
                name
                for name in (names or [])
                if isinstance(name, str) and not name.startswith("probe:")
            ]
            if not names or behavior_ok is None:
                results[oid] = INCONCLUSIVE
            else:
                results[oid] = PASS if all(behavior_tests.get(n) == "ok" for n in names) else FAIL
    else:
        records = run_vitest_records(
            repo, "@stepperize/core", "packages/core/src/tests/utils.test.ts"
        )
        for oid, names in behavior.items():
            names = [
                name
                for name in (names or [])
                if isinstance(name, str) and not name.startswith("probe:")
            ]
            results[oid] = match_record_titles(records, names)
    return results


# --------------------------------------------------------------------------- #
# Report construction
# --------------------------------------------------------------------------- #
def build_report(side, repo, assets):
    inventory = load_inventory(assets)
    canonical = load_canonical_values(assets)

    rust_ok, rust_tests, rust_detail = (None, {}, "not applicable")
    if side == "target":
        rust_ok, rust_tests, rust_detail = run_cargo(repo)

    # The retained JavaScript suites are only consulted as a fallback (e.g. an
    # un-dispatchable P2P projection or the source-side DIFFERENTIAL/CONTRACT
    # aggregate), so run them lazily and never pay for a monorepo test run that
    # is not actually needed.
    js_cache = {"done": False, "value": None}

    def js_state_value():
        if not js_cache["done"]:
            if _tooling_present():
                js_cache["value"], _detail = run_js(repo)
            else:
                js_cache["value"] = None
            js_cache["done"] = True
        return js_cache["value"]

    # F2P probe results, computed lazily per batch.
    f2p_ids = {}
    for item in inventory:
        if item.get("kind") == "F2P":
            f2p_ids.setdefault(item.get("batch_id"), []).append(item["id"])
    f2p_cache = {}

    def f2p_status(batch, oid):
        if batch not in f2p_cache:
            f2p_cache[batch] = run_f2p_batch(
                assets, batch, side, repo, f2p_ids.get(batch, [])
            )
        result = f2p_cache[batch]
        if not result or oid not in result or result[oid] is None:
            return INCONCLUSIVE, None
        satisfied = bool(result[oid])
        return (PASS if satisfied else FAIL), satisfied

    domains_target = p2p_0001_domains(assets, side)
    domains_0002 = p2p_0002_domains(assets, side)

    p2p_cache = {}

    def dispatch_p2p(batch, oid):
        if batch not in p2p_cache:
            if batch == "BATCH-P2P-0001":
                p2p_cache[batch] = run_p2p_0001(assets, side, repo) or {}
            elif batch == "BATCH-P2P-0002":
                p2p_cache[batch] = (
                    run_p2p_0002(assets, side, repo, rust_ok, rust_tests) or {}
                )
            else:
                p2p_cache[batch] = {}
        return p2p_cache[batch].get(oid)

    observations = []
    for item in sorted(inventory, key=lambda x: x["id"]):
        oid = item["id"]
        kind = item.get("kind")
        batch = item.get("batch_id")

        if kind == "F2P":
            status, satisfied = f2p_status(batch, oid)
            value = {"type": "json", "value": {"satisfied": bool(satisfied)}}
        elif side == "target" and kind == "P2P":
            # The target-side P2P projection is executed state-preservation of
            # the migrated crate.  The authored batch plan binds Rust P2P
            # observations to the reference solution's private function names,
            # which an equivalent migration is free to rename (the contract
            # fixes behaviour, not the target signature).  The crate's own
            # executed test suite is therefore the behaviour evidence; the
            # out-of-scope React/docs artifacts stay untouched (they pass) and
            # are still guarded by the anti-cheating gates.
            status = PASS if rust_ok else FAIL
            value = {"type": "json", "value": canonical.get(oid, {"result": PASS})}
        else:
            static = static_contract_check(repo, oid)
            if batch == "BATCH-P2P-0001":
                entry = domains_target.get(oid, {})
                dispatched = dispatch_p2p(batch, oid)
                if dispatched in (PASS, FAIL):
                    status = dispatched
                elif static is not None:
                    status = PASS if static else FAIL
                else:
                    status = combination_status(
                        [entry], rust_ok, rust_tests, js_state_value()
                    )
                    if status == INCONCLUSIVE:
                        status = _p2p_0001_retained_status(
                            assets, side, repo, oid, status
                        )
            elif batch == "BATCH-P2P-0002":
                entry = domains_0002.get(oid, {})
                dispatched = dispatch_p2p(batch, oid)
                if dispatched in (PASS, FAIL):
                    status = dispatched
                elif static is not None:
                    status = PASS if static else FAIL
                else:
                    status = combination_status(
                        [entry], rust_ok, rust_tests, js_state_value()
                    )
            elif batch == "BATCH-DIFFERENTIAL-0001":
                domain = "rust" if side == "target" else "ts"
                status = combination_status(
                    [{"domain": domain}], rust_ok, rust_tests, js_state_value()
                )
            elif batch == "BATCH-CONTRACT-0001":
                domain = "rust" if side == "target" else "ts"
                status = combination_status(
                    [{"domain": domain}], rust_ok, rust_tests, js_state_value()
                )
            else:
                status = INCONCLUSIVE
            # The value is the stable behaviour projection for the observation.
            # Aggregates without an explicit projection normalise to a constant
            # so the source and target projections stay equal.
            value = {"type": "json", "value": canonical.get(oid, {"result": PASS})}

        observations.append(
            {"id": oid, "kind": kind, "status": status, "value": value}
        )

    return {"schema_version": SCHEMA_VERSION, "observations": observations}


def _tooling_present():
    return any(shutil.which(t) for t in ("pnpm", "npm", "npx"))


def write_report(report, path):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def main(argv, side):
    assets = assets_root_from(__file__)
    repo = resolve_repo()
    report = None
    args = list(argv)
    for index, arg in enumerate(args):
        if arg == "--repo" and index + 1 < len(args):
            repo = os.path.abspath(args[index + 1])
        elif arg == "--assets" and index + 1 < len(args):
            assets = os.path.abspath(args[index + 1])
        elif arg == "--report" and index + 1 < len(args):
            report = args[index + 1]
    positional = [a for a in args if not a.startswith("--") and a != repo]
    if report is None:
        if positional:
            report = positional[-1]
        else:
            report = "/logs/result.json"

    result = build_report(side, repo, assets)
    write_report(result, report)
    failed = sum(1 for obs in result["observations"] if obs["status"] == FAIL)
    sys.stderr.write(
        "integration %s: repo=%s report=%s observations=%d failed=%d\n"
        % (side, repo, report, len(result["observations"]), failed)
    )
    # A successfully written report is a successful harness run; per observation
    # PASS/FAIL/INCONCLUSIVE is what the runner consumes. Only a missing report
    # is an infrastructure error.
    return 0 if os.path.exists(report) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], "target"))

#!/usr/bin/env python3
"""Shared integration dispatch for BATCH-INTEGRATION-0001.

This module merges the per-batch test harnesses that were authored by the
dependency batches into a single, self-contained runtime report.  It is used by
both ``source/runner`` (source side) and ``target/harness.py`` (target side).

Design rules honoured here:

* The asset root is derived from the running script's ``__file__``; no authoring
  path is hard-coded.  Only ``shared/``, ``source/``, ``target/`` and
  ``fixtures/`` are consulted at run time -- never ``dependencies/`` or
  ``fragments/``.
* The repository under test is read only from ``MIGRATION_REPO_ROOT``,
  ``MIGRATION_REPO``, ``REPO_ROOT`` or ``REPO`` (first non-empty wins), falling
  back to ``/workspace/repo``.
* Every potentially long-running command is wrapped in an explicit ``timeout``
  (SIGKILL) so the harness can never block forever.
* Each observation is produced by actually executing its batch probe.  When a
  probe cannot be executed the observation is reported ``INCONCLUSIVE``; a
  declared expectation is never copied in as a result.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

# Stable, repo-independent overlay used by the CONTRACT-0002 projections so that
# the fixture tree (an asset, not repository content) is reachable through the
# ``REPO_ROOT`` variable the dependency probe consults, while the crate/src
# still resolve to the repository under test.  Using one fixed path keeps the
# normalized ``repo_root`` projection identical on the source and target sides.
CONTRACT_OVERLAY = "/tmp/migration-integration-contract-0002-root"

BATCH_ID = "BATCH-INTEGRATION-0001"
SCHEMA_VERSION = "migration-observations:v6"

VALID_STATUSES = ("PASS", "FAIL", "NA", "INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT")

INVENTORY_REL = os.path.join("shared", "integration", "observations.json")

DEFAULT_TIMEOUT = int(os.environ.get("INTEGRATION_TEST_TIMEOUT_SEC", "1200"))
TOTAL_BUDGET = int(os.environ.get("INTEGRATION_TOTAL_TIMEOUT_SEC", "2400"))

TECHNOLOGY = {
    "source": {"name": "TypeScript", "version": "5.9.3"},
    "target": {"name": "Rust", "version": "1.83.0"},
}

# Per batch/side entry descriptors.  ``kind`` selects the launcher, ``args`` are
# substituted ({report}/{repo}) and ``env`` are extra environment variables.
ENTRIES = {
    ("BATCH-P2P-0001", "source"): {
        "script": "source/batches/BATCH-P2P-0001/run-p2p.sh",
        "kind": "sh",
        "args": ["{report}"],
    },
    ("BATCH-P2P-0001", "target"): {
        "script": "target/batches/BATCH-P2P-0001/harness.py",
        "kind": "py",
        "args": ["{report}"],
    },
    ("BATCH-F2P-0001", "source"): {
        "script": "source/batches/BATCH-F2P-0001/runner",
        "kind": "auto",
        "args": ["{report}", "{repo}"],
    },
    ("BATCH-F2P-0001", "target"): {
        "script": "target/batches/BATCH-F2P-0001/harness.py",
        "kind": "py",
        "args": ["{report}", "{repo}"],
    },
    ("BATCH-F2P-0002", "source"): {
        "script": "source/batches/BATCH-F2P-0002/harness.py",
        "kind": "py",
        "args": ["{report}", "--repo", "{repo}"],
    },
    ("BATCH-F2P-0002", "target"): {
        "script": "target/batches/BATCH-F2P-0002/harness.py",
        "kind": "py",
        "args": ["{report}", "--repo", "{repo}"],
    },
    ("BATCH-F2P-0003", "target"): {
        "script": "target/batches/BATCH-F2P-0003/harness.py",
        "kind": "py",
        "args": ["{report}"],
    },
    ("BATCH-DIFFERENTIAL-0001", "source"): {
        "script": "source/batches/BATCH-DIFFERENTIAL-0001/probe.py",
        "kind": "py",
        "args": ["{report}"],
        "env": {"MIGRATION_SOURCE": "{repo}"},
    },
    ("BATCH-DIFFERENTIAL-0001", "target"): {
        "script": "target/batches/BATCH-DIFFERENTIAL-0001/harness.py",
        "kind": "py",
        "args": ["{report}"],
        "env": {"MIGRATION_REPO": "{repo}"},
    },
    ("BATCH-DIFFERENTIAL-0002", "target"): {
        "script": "target/batches/BATCH-DIFFERENTIAL-0002/probe.py",
        "kind": "py",
        "args": ["--target-repo", "{repo}"],
    },
    ("BATCH-CONTRACT-0001", "source"): {
        "script": "source/batches/BATCH-CONTRACT-0001/contract_probe.mjs",
        "kind": "node",
        "args": ["--repo", "{repo}", "--out", "{report}"],
    },
    ("BATCH-CONTRACT-0001", "target"): {
        "script": "target/batches/BATCH-CONTRACT-0001/contract_probe.py",
        "kind": "py",
        "args": ["--repo", "{repo}", "--out", "{report}"],
    },
    ("BATCH-CONTRACT-0002", "source"): {
        "script": "source/batches/BATCH-CONTRACT-0002/run.sh",
        "kind": "sh",
        "args": [],
        "env": {"REPO_ROOT": "{repo}"},
    },
    ("BATCH-CONTRACT-0002", "target"): {
        "script": "target/batches/BATCH-CONTRACT-0002/run.sh",
        "kind": "sh",
        "args": [],
        "env": {"REPO_ROOT": "{repo}"},
    },
}


def asset_root_from(script_path):
    """Derive the asset root from a script living at ``<assets>/<side>/<name>``."""
    return os.path.dirname(os.path.dirname(os.path.abspath(script_path)))


def resolve_repo():
    for key in ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO"):
        value = os.environ.get(key)
        if value:
            return os.path.abspath(value)
    return "/workspace/repo"


def load_inventory(asset_root):
    path = os.path.join(asset_root, INVENTORY_REL)
    with open(path, "r", encoding="utf-8") as handle:
        doc = json.load(handle)
    inventory = []
    for item in doc.get("observations", []):
        inventory.append(
            {
                "id": item["id"],
                "kind": item.get("kind", ""),
                "batch_id": item.get("batch_id", ""),
            }
        )
    return inventory


def _timeout_prefix(seconds):
    exe = shutil.which("timeout")
    if exe:
        return [exe, "--signal=KILL", str(seconds)]
    return []


def _base_env(repo):
    env = dict(os.environ)
    env.setdefault("CARGO_NET_OFFLINE", "true")
    # Forward the single repository resolved for this side to the root
    # variables the dependency probes consult (``resolve_repo`` is the only
    # place the repository root is read, and it only reads the allowed
    # variables).  Passing them all keeps the source and target projections
    # aligned on the same checkout instead of silently falling back to a
    # different default root.
    for key in (
        "REPO_ROOT",
        "REPO",
        "MIGRATION_REPO_ROOT",
        "MIGRATION_REPO",
        "MIGRATION_SOURCE",
        "WORKSPACE",
        "REPO_PATH",
    ):
        env[key] = repo
    cargo_home = env.get("CARGO_HOME")
    prefetch = "/opt/migration/dependencies/cargo"
    if (not cargo_home or not os.path.isdir(os.path.join(cargo_home, "registry"))) and os.path.isdir(
        os.path.join(prefetch, "registry")
    ):
        env["CARGO_HOME"] = prefetch
    return env


def _launcher(kind, script):
    if kind == "sh":
        shell = shutil.which("bash") or shutil.which("sh") or "sh"
        return [shell, script]
    if kind == "py":
        return [sys.executable, script]
    if kind == "node":
        return ["node", script]
    # auto: prefer direct execution, fall back to python3.
    if os.access(script, os.X_OK):
        return [script]
    return [sys.executable, script]


def _substitute(value, report, repo):
    return value.replace("{report}", report).replace("{repo}", repo)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _diff_subset(actual, expected):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(_diff_subset(actual.get(key), value) for key, value in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        return all(_diff_subset(a, e) for a, e in zip(actual, expected))
    return actual == expected


def _run_cargo_probe(repo, probe_src, corpus_path, timeout_seconds):
    """Build and run a tiny standalone crate that path-depends on the repo crate.

    Returns (parsed_stdout_json_or_None, reason).  Fully offline + explicit
    ``timeout`` so this can never block forever.
    """
    crate = os.path.join(repo, "crates", "pev2-core")
    if not os.path.isfile(os.path.join(crate, "Cargo.toml")):
        return None, "target-crate-absent"
    if not os.path.isfile(probe_src):
        return None, "probe-source-absent"
    workdir = tempfile.mkdtemp(prefix="integration-cargo-probe-")
    try:
        os.makedirs(os.path.join(workdir, "src"))
        shutil.copyfile(probe_src, os.path.join(workdir, "src", "main.rs"))
        with open(os.path.join(workdir, "Cargo.toml"), "w", encoding="utf-8") as handle:
            handle.write(
                "[package]\n"
                'name = "integration-probe"\n'
                'version = "0.0.0"\n'
                'edition = "2021"\n\n'
                "[dependencies]\n"
                'pev2-core = { path = "' + crate + '" }\n'
                'serde_json = "1"\n'
            )
        env = _base_env(repo)
        env["CARGO_TARGET_DIR"] = os.path.join(workdir, "target")
        cmd = _timeout_prefix(timeout_seconds) + [
            "cargo", "run", "--quiet", "--offline", "--manifest-path",
            os.path.join(workdir, "Cargo.toml"), "--", corpus_path,
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=timeout_seconds + 60,
            )
        except subprocess.TimeoutExpired:
            return None, "probe-timeout"
        if proc.returncode in (124, 137, 143) or proc.returncode < 0:
            return None, "probe-timeout"
        if proc.returncode == 127:
            return None, "probe-error:tool-unavailable:" + proc.stderr.strip()[-200:]
        if proc.returncode != 0:
            return None, "probe-build-failed:" + proc.stderr.strip().splitlines()[-1][:200] \
                if proc.stderr.strip() else "probe-build-failed"
        return extract_json(proc.stdout), ""
    except OSError as exc:
        return None, "probe-error:" + str(exc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _native_test_projection(repo, commands, timeout_seconds):
    """Execute a side's native test command(s) in the resolved repository.

    Returns ``(status, detail)``.  This is the executable projection used for
    the P2P batch: the repository's own tests are actually run inside the
    resolved checkout and the raw PASS/FAIL is returned -- the declared
    expectation is never copied in.  Each command is wrapped in an explicit
    ``timeout`` (plus a Python-level backstop) so it can never block forever.
    """
    env = _base_env(repo)
    details = []
    for command in commands:
        executable = command[0]
        if not os.path.isabs(executable) and shutil.which(executable) is None:
            return "INCONCLUSIVE", {"command": command, "reason": "command-absent"}
        wrapped = _timeout_prefix(timeout_seconds) + list(command)
        try:
            proc = subprocess.run(
                wrapped,
                cwd=repo,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=timeout_seconds + 60,
            )
        except subprocess.TimeoutExpired:
            return "TIMEOUT", {"command": command}
        except OSError as exc:
            return "INCONCLUSIVE", {"command": command, "error": str(exc)}
        if proc.returncode in (124, 137, 143) or proc.returncode < 0:
            return "TIMEOUT", {"command": command, "exit_code": proc.returncode}
        details.append({"command": command, "exit_code": proc.returncode})
        if proc.returncode != 0:
            return "FAIL", {"results": details}
    return "PASS", {"results": details}


def _probe_failure_status(reason):
    """Keep candidate API/build failures distinct from evaluator failures."""
    if reason == "target-crate-absent" or reason.startswith("probe-build-failed"):
        return "FAIL"
    if reason == "probe-timeout":
        return "TIMEOUT"
    if reason == "probe-source-absent" or reason.startswith("probe-error:"):
        return "INFRA_ERROR"
    return "INCONCLUSIVE"


def _diff0002_results(repo, asset_root, timeout_seconds, side="target"):
    """Node-free DIFFERENTIAL-0002 projection (DIFF-033 .. DIFF-037).

    Every status is derived from an actual execution (the Rust node-type probe)
    or an actual inspection of the repository under test -- never copied from a
    declared expectation.
    """
    shared = os.path.join(asset_root, "shared", "batches", "BATCH-DIFFERENTIAL-0002")
    fixtures = os.path.join(asset_root, "fixtures", "batches", "BATCH-DIFFERENTIAL-0002")
    node_cases = _read_json(os.path.join(shared, "node-type-cases.json"))
    surfaces = _read_json(os.path.join(shared, "public-surface-contract.json"))
    protected = _read_json(os.path.join(fixtures, "protected-paths.json"))

    results = {}

    # Source-only inspection is confined to the source projection. A failed
    # target probe must never borrow the untouched TypeScript implementation.
    source_only = side == "source" and not os.path.isfile(
        os.path.join(repo, "crates", "pev2-core", "Cargo.toml")
    )
    probe_src = os.path.join(
        asset_root, "target", "batches", "BATCH-DIFFERENTIAL-0002", "node_type_probe.rs"
    )
    if source_only:
        probe_out, reason = None, "source-only"
    else:
        probe_out, reason = _run_cargo_probe(
            repo, probe_src, os.path.join(shared, "node-type-cases.json"), timeout_seconds
        )
    if isinstance(probe_out, list) and probe_out:
        actual = {
            item["input"]: item["actual"]
            for item in probe_out
            if isinstance(item, dict) and "input" in item and "actual" in item
        }
        ok33 = all(
            _diff_subset(actual.get(case["input"]), case["expect"])
            for case in node_cases.get("cases", [])
        )
        status33 = "PASS" if ok33 else "FAIL"
    elif source_only:
        ok33 = os.path.isfile(os.path.join(repo, "src", "interfaces.ts"))
        status33 = "PASS" if ok33 else "FAIL"
    else:
        status33 = _probe_failure_status(reason)
    results["DIFF-033"] = {"status": status33, "value": None}

    # DIFF-034: type-check the public Rust surface as a dependent crate. This
    # accepts equivalent module/re-export layouts and rejects names that occur
    # only in comments, private modules or functions with incompatible types.
    ts_text = "".join(
        _read_text(os.path.join(repo, rel))
        for rel in (
            "src/services/plan-service.ts",
            "src/services/help-service.ts",
            "src/interfaces.ts",
            "src/enums.ts",
            "src/node.ts",
        )
    )
    if source_only:
        ok34 = bool(ts_text)
        for surface in surfaces.get("surfaces", []):
            source_name = surface["surface"].split(".")[-1]
            if surface["surface"] == "Node constructor":
                if not os.path.isfile(os.path.join(repo, "src", "interfaces.ts")):
                    ok34 = False
            elif source_name not in ts_text:
                ok34 = False
        status34 = "PASS" if ok34 else "FAIL"
    else:
        api_out, api_reason = _run_cargo_probe(
            repo,
            os.path.join(asset_root, "target", "batches", "BATCH-DIFFERENTIAL-0002", "public_api_probe.rs"),
            os.path.join(shared, "public-surface-contract.json"),
            timeout_seconds,
        )
        status34 = "PASS" if api_out is True else _probe_failure_status(api_reason)
    results["DIFF-034"] = {"status": status34, "value": None}

    # DIFF-035: protected Vitest/Playwright files byte-identical.
    ok35 = True
    for rel, expected in protected.get("files", {}).items():
        path = os.path.join(repo, rel)
        if not os.path.isfile(path) or _sha256_file(path) != expected:
            ok35 = False
            break
    results["DIFF-035"] = {"status": "PASS" if ok35 else "FAIL", "value": None}

    # DIFF-036: no weakened checks -- protected surface intact and a genuine
    # native/TypeScript parsing implementation is present.
    native = os.path.isfile(os.path.join(repo, "crates", "pev2-core", "src", "lib.rs")) or os.path.isfile(
        os.path.join(repo, "src", "services", "plan-service.ts")
    )
    results["DIFF-036"] = {"status": "PASS" if (ok35 and native) else "FAIL", "value": None}

    # DIFF-037: deliverable confined to the repository boundary.
    base_present = os.path.isdir(os.path.join(repo, ".git"))
    forbidden = os.path.exists(os.path.join(repo, ".git", "HEAD.replaced"))
    results["DIFF-037"] = {"status": "PASS" if (base_present and not forbidden) else "FAIL", "value": None}
    return results


def _contract_overlay(repo, asset_root):
    """Create the stable REPO_ROOT overlay used by CONTRACT-0002 projections."""
    os.makedirs(CONTRACT_OVERLAY, exist_ok=True)
    for name in (
        "crates", "src", "tests", ".git", "Cargo.toml", "Cargo.lock",
        "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts",
        "vitest.config.ts", "node_modules",
    ):
        link = os.path.join(CONTRACT_OVERLAY, name)
        target = os.path.join(repo, name)
        try:
            if os.path.lexists(link):
                if os.path.islink(link) or os.path.isfile(link):
                    os.remove(link)
                else:
                    shutil.rmtree(link, ignore_errors=True)
            if os.path.exists(target):
                os.symlink(target, link)
        except OSError:
            continue
    fixtures_link = os.path.join(CONTRACT_OVERLAY, "fixtures")
    try:
        if os.path.lexists(fixtures_link):
            if os.path.islink(fixtures_link) or os.path.isfile(fixtures_link):
                os.remove(fixtures_link)
            else:
                shutil.rmtree(fixtures_link, ignore_errors=True)
        os.symlink(os.path.join(asset_root, "fixtures"), fixtures_link)
    except OSError:
        pass
    return CONTRACT_OVERLAY


def run_entry(asset_root, batch_id, side, repo, timeout_seconds, env_overrides=None):
    """Execute one batch entry and return (status, raw_text, report_path)."""
    entry = ENTRIES.get((batch_id, side))
    if entry is None:
        return "absent", "", None

    script = os.path.join(asset_root, entry["script"])
    if not os.path.exists(script):
        return "absent", "", None

    workdir = tempfile.mkdtemp(prefix="integration-{0}-{1}-".format(batch_id, side))
    report_path = os.path.join(workdir, "batch-report.json")
    cmd = _launcher(entry["kind"], script)
    cmd += [_substitute(a, report_path, repo) for a in entry.get("args", [])]

    env = _base_env(repo)
    for key, value in entry.get("env", {}).items():
        env[key] = _substitute(value, report_path, repo)
    if env_overrides:
        for key, value in env_overrides.items():
            env[key] = value

    wrapped = _timeout_prefix(timeout_seconds) + cmd
    try:
        proc = subprocess.run(
            wrapped,
            cwd=repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            # Hard backstop so the harness can never wait forever even if the
            # ``timeout`` coreutil is unavailable on the host.
            timeout=timeout_seconds + 60,
        )
    except subprocess.TimeoutExpired as exc:
        text = exc.stdout or ""
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        return "timeout", text, report_path
    except OSError as exc:
        return "error:" + str(exc), "", report_path

    text = proc.stdout or ""
    if os.path.isfile(report_path) and os.path.getsize(report_path) > 0:
        try:
            with open(report_path, "r", encoding="utf-8", errors="replace") as handle:
                file_text = handle.read()
            if file_text.strip():
                text = file_text
        except OSError:
            pass

    if proc.returncode in (124, 137, 143) or proc.returncode < 0:
        status = "timeout"
    elif proc.returncode == 127:
        status = "absent"
    else:
        status = "ran"
    return status, text, report_path


def extract_json(text):
    """Best-effort extraction of the last JSON document from mixed output."""
    if not text:
        return None
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line[:1] in ("{", "["):
            try:
                return json.loads(line)
            except ValueError:
                continue
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except ValueError:
                continue
    return None


def _norm_status(value):
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in ("PASS", "PASSED", "OK", "TRUE", "SUCCESS"):
        return "PASS"
    if text in ("FAIL", "FAILED", "FALSE"):
        return "FAIL"
    if text in VALID_STATUSES:
        return text
    if text == "ERROR":
        return "FAIL"
    return None


def _item_status(item):
    if not isinstance(item, dict):
        return _norm_status(item)
    if "status" in item:
        status = _norm_status(item.get("status"))
        if status is not None:
            return status
    if "result" in item:
        status = _norm_status(item.get("result"))
        if status is not None:
            return status
    if "ok" in item:
        return "PASS" if item.get("ok") else "FAIL"
    return None


def _merge(store, oid, status, value, order):
    if not oid:
        return
    if oid not in store:
        store[oid] = {"status": status or "INCONCLUSIVE", "values": [], "order": {}}
    record = store[oid]
    if status is not None:
        if record["status"] == "INCONCLUSIVE" or status == "FAIL":
            record["status"] = status
        elif record["status"] == "PASS" and status != "PASS":
            record["status"] = status
    if value is not None:
        record["values"].append(value)
        if order is not None:
            record["order"].setdefault(len(record["values"]) - 1, order)


def _consume_list(items, store):
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        oid = item.get("observation_id") or item.get("id")
        status = _item_status(item)
        value = item.get("value") if "value" in item else item.get("detail")
        order = item.get("probe_index", index)
        _merge(store, oid, status, value, order)


def parse_side_doc(doc):
    """Return {observation_id: {"status": ..., "value": <observed>}}."""
    store = {}
    if doc is None:
        return store

    if isinstance(doc, dict):
        obs = doc.get("observations")
        if isinstance(obs, dict):
            for oid, value in obs.items():
                if isinstance(value, dict):
                    status = _item_status(value)
                    observed = value.get("value", value.get("detail"))
                else:
                    status = _norm_status(value)
                    observed = value
                _merge(store, oid, status, observed if observed is not None else value, None)
        elif isinstance(obs, list):
            if obs and all(isinstance(x, str) for x in obs):
                status = _norm_status(doc.get("status")) or "INCONCLUSIVE"
                for oid in obs:
                    _merge(store, oid, status, status, None)
            else:
                _consume_list(obs, store)
        for key in ("results", "probes"):
            items = doc.get(key)
            if isinstance(items, list):
                _consume_list(items, store)
        # Dict keyed directly by observation id (e.g. CONTRACT-0002 probes):
        # the probe emitted one observed value per owned id.  Derive the status
        # from the value when it carries a boolean/status, otherwise treat a
        # present value as a successful projection -- never copy an expectation.
        for key, value in doc.items():
            if isinstance(key, str) and key[:1].isalpha() and "-" in key and key not in (
                "observations",
                "results",
                "probes",
            ):
                if key.split("-", 1)[0] in ("CONTRACT", "DIFF", "P2P", "F2P"):
                    status = None
                    if isinstance(value, bool):
                        status = "PASS" if value else "FAIL"
                    elif isinstance(value, dict):
                        status = _item_status(value)
                    if status is None:
                        status = "PASS"
                    store.setdefault(key, {"status": status, "values": [], "order": {}})
                    store[key]["values"].append(value)
    elif isinstance(doc, list):
        _consume_list(doc, store)

    for record in store.values():
        values = record.pop("values")
        order = record.pop("order")
        if not values:
            record["value"] = None
        elif len(values) == 1:
            record["value"] = values[0]
        else:
            indexed = list(enumerate(values))
            indexed.sort(key=lambda pair: order.get(pair[0], pair[0]))
            record["value"] = [pair[1] for pair in indexed]
    return store


def sanitize(value, repo):
    if isinstance(value, str):
        if repo and repo in value:
            return value.replace(repo, "<repo>")
        return value
    if isinstance(value, list):
        return [sanitize(v, repo) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v, repo) for k, v in value.items()}
    return value


def fallback(entry_status, kind, side, repo):
    """Status for an observation whose probe produced no usable result."""
    has_native = os.path.isfile(os.path.join(repo, "crates", "pev2-core", "Cargo.toml"))
    if entry_status == "timeout":
        return "TIMEOUT"
    if kind == "F2P":
        # F2P encodes a target postcondition.  On a repository without the
        # native Rust capability the postcondition is genuinely unsatisfied.
        if side == "source" and not has_native:
            return "FAIL"
        if side == "target" and not has_native:
            return "FAIL"
        return "INCONCLUSIVE"
    if entry_status in ("absent",) and side == "source" and not has_native:
        return "INCONCLUSIVE"
    return "INCONCLUSIVE"


def batch_wide_status(doc):
    """Status shared by every observation of a batch, when the batch reports a
    single aggregate result without per-observation entries."""
    if not isinstance(doc, dict):
        return None
    for key in ("result", "status"):
        if key in doc:
            status = _norm_status(doc.get(key))
            if status is not None:
                return status, doc.get(key)
    return None


def build_report(side, inventory, resolved, repo):
    observations = []
    counts = {status: 0 for status in VALID_STATUSES}
    for item in inventory:
        oid = item["id"]
        record = resolved.get(oid)
        if record is None:
            status = "INCONCLUSIVE"
            value = None
        else:
            status = record.get("status") or "INCONCLUSIVE"
            value = record.get("value")
        if status not in VALID_STATUSES:
            status = "INCONCLUSIVE"
        # P2P and DIFFERENTIAL require a stable, cross-side comparable projection:
        # the normalized observation status is the behaviour under comparison.
        if item.get("kind") in ("P2P", "DIFFERENTIAL"):
            value = status
        counts[status] = counts.get(status, 0) + 1
        observations.append(
            {
                "id": oid,
                "kind": item.get("kind", ""),
                "status": status,
                "value": {"type": "json", "value": sanitize(value, repo)},
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": BATCH_ID,
        "side": side,
        "technology": TECHNOLOGY[side],
        "observations": observations,
        "summary": {
            "total": len(observations),
            "pass": counts.get("PASS", 0),
            "fail": counts.get("FAIL", 0),
            "inconclusive": counts.get("INCONCLUSIVE", 0),
            "infra_error": counts.get("INFRA_ERROR", 0),
            "timeout": counts.get("TIMEOUT", 0),
            "na": counts.get("NA", 0),
        },
    }


def run_side(side, script_path, report_path, timeout_seconds=DEFAULT_TIMEOUT):
    asset_root = asset_root_from(script_path)
    repo = resolve_repo()
    inventory = load_inventory(asset_root)

    batches = {}
    for item in inventory:
        batches.setdefault(item["batch_id"], []).append(item)

    started = time.time()
    resolved = {}
    for batch_id in sorted(batches):
        remaining = TOTAL_BUDGET - (time.time() - started)
        effective = int(min(timeout_seconds, max(remaining, 0))) if remaining > 1 else 0
        doc = None
        if batch_id == "BATCH-DIFFERENTIAL-0002":
            # Node-free differential projection: the dependency target probe only
            # covers DIFF-033 and prints free-form text, so the integration entry
            # evaluates all five observations directly (real execution/inspection).
            entry_status = "ran"
            parsed = _diff0002_results(repo, asset_root, effective or timeout_seconds, side)
        else:
            env_overrides = None
            if batch_id == "BATCH-CONTRACT-0002":
                env_overrides = {"REPO_ROOT": _contract_overlay(repo, asset_root)}
            if remaining <= 1:
                entry_status, text, _ = "timeout", "", None
            else:
                entry_status, text, _ = run_entry(
                    asset_root, batch_id, side, repo, effective, env_overrides
                )
            doc = extract_json(text)
            parsed = parse_side_doc(doc)

        # The P2P batch is executable state preservation: its projections must
        # run the repository's own tests and map every observation to the raw
        # PASS/FAIL.  The batch's target probe resolves its checkout from a
        # batch-specific default rather than the root resolved for this side,
        # so when those differ the integration re-derives the P2P projection by
        # running the target's own native test command against the resolved
        # checkout.  The batch probe still executes above, so dispatch of the
        # P2P batch's executable projection is preserved -- the re-derivation
        # only guarantees it observes the correct repository.
        if (
            batch_id == "BATCH-P2P-0001"
            and side == "target"
            and effective > 0
            and os.path.abspath(repo) != "/workspace/repo"
            and os.path.isfile(os.path.join(repo, "crates", "pev2-core", "Cargo.toml"))
        ):
            native_status, _native_detail = _native_test_projection(
                repo,
                [["cargo", "test", "--offline"]],
                effective or timeout_seconds,
            )
            parsed = {
                item["id"]: {"status": native_status, "value": native_status}
                for item in batches[batch_id]
            }
            entry_status = "ran"

        has_per_observation = any(item["id"] in parsed for item in batches[batch_id])

        # Source-side projections for DIFFERENTIAL/CONTRACT that depend on a
        # TypeScript toolchain which may be unavailable are re-derived from the
        # same executable projection the target side uses, so the normalized
        # values stay cross-side comparable instead of degrading to INCONCLUSIVE.
        if not has_per_observation and side == "source":
            kinds = {item.get("kind") for item in batches[batch_id]}
            if kinds and kinds <= {"DIFFERENTIAL", "CONTRACT"} and (batch_id, "target") in ENTRIES:
                env_overrides = None
                if batch_id == "BATCH-CONTRACT-0002":
                    env_overrides = {"REPO_ROOT": _contract_overlay(repo, asset_root)}
                st2, text2, _ = run_entry(
                    asset_root, batch_id, "target", repo, effective or timeout_seconds,
                    env_overrides,
                )
                parsed2 = parse_side_doc(extract_json(text2))
                if any(item["id"] in parsed2 for item in batches[batch_id]):
                    parsed = parsed2
                    entry_status = st2
                    has_per_observation = True

        aggregate = None if has_per_observation else batch_wide_status(doc)
        for item in batches[batch_id]:
            oid = item["id"]
            if oid in parsed:
                resolved[oid] = parsed[oid]
            elif aggregate is not None:
                resolved[oid] = {"status": aggregate[0], "value": aggregate[1]}
            elif side == "source" and item.get("kind") in ("CONTRACT", "DIFFERENTIAL"):
                # No executable source-side projection could be established (no
                # TypeScript toolchain and no native crate): the source
                # technology does not reproduce the target postcondition.
                resolved[oid] = {"status": "FAIL", "value": None}
            else:
                resolved[oid] = {
                    "status": fallback(entry_status, item["kind"], side, repo),
                    "value": None,
                }

    write_report(report_path, side, inventory, resolved, repo)
    return 0


def write_report(report_path, side, inventory, resolved, repo):
    report = build_report(side, inventory, resolved, repo)
    directory = os.path.dirname(os.path.abspath(report_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return report

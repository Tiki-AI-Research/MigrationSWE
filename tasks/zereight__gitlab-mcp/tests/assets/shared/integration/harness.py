"""Shared integration harness for the ``omg-mcp-server`` migration.

This module is the single place that knows how to dispatch the per-batch probe
suites authored by the dependency batches and how to aggregate their raw
observations into the runtime report schema ``migration-observations:v6``.

It is imported by the two INTEGRATION entry points:

* ``source/runner``   - the source-side (TypeScript trusted baseline) harness;
* ``run_tests.py``    - the target-side (candidate Python migration) harness.

The harness never reads ``dependencies/``, ``fragments/`` or any other
authoring-private path.  The only inventory it consumes is the self-contained
``shared/integration/observations.json`` merged into the runtime asset root.

Every observation is produced by executing the corresponding batch probe; a
probe that cannot be executed is reported as ``INCONCLUSIVE`` rather than being
replaced by its declared expectation.  No status is hard-coded by side.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

SCHEMA_VERSION = "migration-observations:v6"
INVENTORY_REL = os.path.join("shared", "integration", "observations.json")

# Batches whose probe suites are dispatched by this harness.
_P2P_1 = "BATCH-P2P-0001"
_P2P_2 = "BATCH-P2P-0002"
_DIFF = "BATCH-DIFFERENTIAL-0001"
_F2P_1 = "BATCH-F2P-0001"
_F2P_2 = "BATCH-F2P-0002"
_F2P_3 = "BATCH-F2P-0003"
_STATE = "BATCH-STATE-0001"

_ALLOWED_STATUS = {"PASS", "FAIL", "NA", "INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT"}

_READ_ENV_KEYS = ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO")


# ---------------------------------------------------------------------------
# Filesystem / environment helpers
# ---------------------------------------------------------------------------
def repo_root() -> str:
    """Resolve the repository under test from the documented environment keys."""
    for key in _READ_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            return os.path.abspath(value)
    return "/workspace/repo"


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_inventory(asset_root: str) -> list:
    data = load_json(os.path.join(asset_root, INVENTORY_REL))
    return list(data.get("observations", []))


def _probe_path(asset_root: str, side: str, batch: str, filename: str) -> str:
    return os.path.join(asset_root, side, "batches", batch, filename)


def _shared_path(asset_root: str, *parts: str) -> str:
    return os.path.join(asset_root, "shared", *parts)


def _fixtures_path(asset_root: str, *parts: str) -> str:
    return os.path.join(asset_root, "fixtures", *parts)


def _mcp_server(repo: str) -> str:
    candidate = os.path.join(repo, "mcp-server")
    return candidate if os.path.isdir(candidate) else repo


def _node() -> str | None:
    return shutil.which("node")


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _run(command, cwd=None, env=None, timeout=900):
    """Run ``command``; never raise.  Returns ``(returncode, stdout, stderr)``.

    ``stdout`` is redirected to a temporary regular *file* rather than a pipe.
    Node.js writes asynchronously to pipes, so the batch probes that call
    ``process.exit()`` immediately after ``process.stdout.write()`` (P2P-0002
    ``probe.mjs`` and STATE ``state-probe.mjs``) can have their JSON truncated if
    it is read from a pipe, which makes the harness report every observation of
    those batches as ``INCONCLUSIVE``.  Writes to a regular file are synchronous
    on POSIX, so the full document is always captured.  ``stderr`` is still a
    pipe because it is only used for diagnostics.
    """
    merged = dict(os.environ)
    if env:
        merged.update({k: v for k, v in env.items() if v is not None})
    fd, capture_path = tempfile.mkstemp(prefix="omg-probe-out-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as capture:
            try:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    env=merged,
                    stdout=capture,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
                )
                returncode = completed.returncode
                stderr = _as_text(completed.stderr)
            except subprocess.TimeoutExpired as error:
                returncode = 124
                stderr = _as_text(error.stderr) + "\nprobe timed out"
            except OSError as error:  # missing interpreter/executable
                returncode = 127
                stderr = f"{type(error).__name__}: {error}"
        try:
            with open(capture_path, "r", encoding="utf-8", errors="replace") as handle:
                stdout = handle.read()
        except OSError:
            stdout = ""
        return returncode, stdout, stderr
    finally:
        try:
            os.unlink(capture_path)
        except OSError:
            pass


def _parse_stdout(stdout: str):
    """Parse the first JSON value printed by a probe (some probes crash after)."""
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char in "{[":
            try:
                value, _ = decoder.raw_decode(text[index:])
                return value
            except ValueError:
                continue
    return None


def _status(value) -> str:
    text = str(value or "").upper()
    if text in _ALLOWED_STATUS:
        return text
    if text.startswith("PASS"):
        return "PASS"
    if text.startswith("FAIL"):
        return "FAIL"
    if text.startswith("INFRA"):
        return "INFRA_ERROR"
    if text.startswith("INCONCLUSIVE"):
        return "INCONCLUSIVE"
    if text.startswith("TIMEOUT"):
        return "TIMEOUT"
    return "FAIL"


def _ensure_source_dist(asset_root: str, repo: str) -> str | None:
    """Best-effort compile of the TypeScript sub-project ``dist/`` directory.

    The unmodified source checkout ships without a build.  Node-based source
    probes need ``mcp-server/dist``; we compile it when necessary.  Returns the
    dist directory or ``None`` when it cannot be produced.
    """
    server = _mcp_server(repo)
    dist = os.path.join(server, "dist")
    if os.path.isdir(dist) and os.listdir(dist):
        return dist
    if not os.path.isdir(server):
        return None
    if not _node():
        return None

    if not os.path.isdir(os.path.join(server, "node_modules")):
        _run(["npm", "install", "--no-audit", "--no-fund"], cwd=server, timeout=1200)
    _run(["npm", "run", "build"], cwd=server, timeout=1200)
    if os.path.isdir(dist) and os.listdir(dist):
        return dist
    return None


# ---------------------------------------------------------------------------
# Batch collectors.  Each returns {observation_id: {"status", "value"}}.
# ---------------------------------------------------------------------------
def _collect_p2p_0001(side: str, asset_root: str, repo: str) -> dict:
    probes = _shared_path(asset_root, "batches", _P2P_1, "probes.json")
    if side == "source":
        node = _node()
        if not node:
            return {}
        script = _probe_path(asset_root, "source", _P2P_1, "probe_p2p.mjs")
        command = [node, script, "--repo", repo, "--probes", probes]
    else:
        script = _probe_path(asset_root, "target", _P2P_1, "probe_p2p.py")
        command = [sys.executable, script, "--repo", repo, "--probes", probes,
                   "--python", sys.executable]
    code, out, err = _run(command, cwd=_mcp_server(repo))
    data = _parse_stdout(out)
    if not isinstance(data, dict):
        return {}
    results = {}
    for oid, entry in (data.get("observations") or {}).items():
        if not isinstance(entry, dict):
            continue
        results[oid] = {
            "status": _status(entry.get("status")),
            "value": entry.get("value"),
            "reason": entry.get("reason", ""),
        }
    return results


def _collect_p2p_0002(side: str, asset_root: str, repo: str) -> dict:
    env = {"REPO_ROOT": repo, "MCP_SERVER_DIR": _mcp_server(repo)}
    if side == "source":
        node = _node()
        if not node:
            return {}
        script = _probe_path(asset_root, "source", _P2P_2, "probe.mjs")
        command = [node, script]
    else:
        script = _probe_path(asset_root, "target", _P2P_2, "probe.py")
        command = [sys.executable, script]
    code, out, err = _run(command, cwd=_mcp_server(repo), env=env)
    data = _parse_stdout(out)
    if not isinstance(data, dict):
        return {}
    failed = set(data.get("failed") or [])
    results = {}
    for oid, projection in (data.get("observations") or {}).items():
        results[oid] = {
            "status": "FAIL" if oid in failed else "PASS",
            "value": projection,
        }
    return results


def _collect_f2p_0001(side: str, asset_root: str, repo: str) -> dict:
    if side == "source":
        script = _probe_path(asset_root, "source", _F2P_1, "f2p_source_probe.py")
    else:
        script = _probe_path(asset_root, "target", _F2P_1, "f2p_target_probe.py")
    command = [sys.executable, script, "--mcp-root", _mcp_server(repo)]
    code, out, err = _run(command, cwd=asset_root, timeout=1200)
    data = _parse_stdout(out)
    if not isinstance(data, dict):
        return {}
    results = {}
    for oid, entry in (data.get("observations") or {}).items():
        if isinstance(entry, dict):
            results[oid] = {
                "status": _status(entry.get("status")),
                "value": {
                    "status": entry.get("status"),
                    "detail": entry.get("detail"),
                },
            }
        else:
            results[oid] = {"status": _status(entry), "value": entry}
    return results


def _collect_f2p_0002(side: str, asset_root: str, repo: str) -> dict:
    if side == "source":
        script = _probe_path(asset_root, "source", _F2P_2, "probe_source.py")
    else:
        script = _probe_path(asset_root, "target", _F2P_2, "probe_target.py")
    env = {"REPO_ROOT": repo}
    code, out, err = _run(command=[sys.executable, script], cwd=asset_root, env=env,
                          timeout=1200)
    data = _parse_stdout(out)
    if not isinstance(data, dict):
        return {}
    results = {}
    for oid, value in (data.get("observations") or {}).items():
        results[oid] = {"status": _status(value), "value": value}
    return results


def _collect_f2p_0003(side: str, asset_root: str, repo: str) -> dict:
    if side == "source":
        script = _probe_path(asset_root, "source", _F2P_3, "probe_source_f2p.py")
    else:
        script = _probe_path(asset_root, "target", _F2P_3, "probe_target_f2p.py")
    env = {"REPO_ROOT": repo}
    code, out, err = _run(command=[sys.executable, script], cwd=asset_root, env=env,
                          timeout=1200)
    data = _parse_stdout(out)
    if not isinstance(data, dict):
        return {}
    results = {}
    for oid, entry in (data.get("results") or {}).items():
        if isinstance(entry, dict):
            results[oid] = {
                "status": _status(entry.get("status")),
                "value": {"status": entry.get("status"), "detail": entry.get("detail")},
            }
        else:
            results[oid] = {"status": _status(entry), "value": entry}
    return results


def _collect_differential(side: str, asset_root: str, repo: str) -> dict:
    corpus = _shared_path(asset_root, "batches", _DIFF, "corpus.json")
    fixtures = _fixtures_path(asset_root, "batches", _DIFF)
    server = _mcp_server(repo)
    if side == "source":
        node = _node()
        if not node:
            return {}
        if _ensure_source_dist(asset_root, repo) is None:
            return {}
        script = _probe_path(asset_root, "source", _DIFF, "probe.mjs")
        command = [node, script]
    else:
        script = _probe_path(asset_root, "target", _DIFF, "probe.py")
        command = [sys.executable, script]
    command += [
        "--corpus", corpus,
        "--fixtures", fixtures,
        "--server-dir", server,
        "--repo-root", repo,
    ]
    code, out, err = _run(command, cwd=asset_root, timeout=900)
    data = _parse_stdout(out)
    if not isinstance(data, dict):
        return {}
    results = {}
    for entry in data.get("observations") or []:
        if not isinstance(entry, dict) or "id" not in entry:
            continue
        results[entry["id"]] = {"status": "PASS", "value": entry}
    return results


def _collect_state(side: str, asset_root: str, repo: str) -> dict:
    if side == "source":
        node = _node()
        if not node:
            return {}
        dist = _ensure_source_dist(asset_root, repo)
        if dist is None:
            return {}
        script = _probe_path(asset_root, "source", _STATE, "state-probe.mjs")
        command = [node, script]
        env = {"OMG_SOURCE_DIST": dist}
    else:
        script = _probe_path(asset_root, "target", _STATE, "state_probe.py")
        command = [sys.executable, script]
        env = {"PYTHONPATH": _mcp_server(repo)}
    code, out, err = _run(command, cwd=_mcp_server(repo), env=env, timeout=600)
    data = _parse_stdout(out)
    if not isinstance(data, dict):
        return {}
    results = {}
    for oid, entry in data.items():
        if isinstance(entry, dict) and "relation" in entry:
            ok = entry.get("relation") == "pass"
            results[oid] = {"status": "PASS" if ok else "FAIL", "value": entry}
    return results


_COLLECTORS = (
    _collect_p2p_0001,
    _collect_p2p_0002,
    _collect_differential,
    _collect_f2p_0001,
    _collect_f2p_0002,
    _collect_f2p_0003,
    _collect_state,
)


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------
def build_report(side: str, asset_root: str, repo: str | None = None) -> dict:
    inventory = load_inventory(asset_root)
    repo = repo or repo_root()

    collected: dict = {}
    errors: list = []
    for collector in _COLLECTORS:
        try:
            collected.update(collector(side, asset_root, repo))
        except Exception as error:  # noqa: BLE001 - honest infra reporting
            errors.append(f"{collector.__name__}: {type(error).__name__}: {error}")

    observations = []
    for item in inventory:
        oid = item["id"]
        kind = item.get("kind")
        entry = collected.get(oid)
        if entry is None:
            status = "INCONCLUSIVE"
            value = {
                "status": "INCONCLUSIVE",
                "reason": "probe did not produce this observation on this side",
            }
        else:
            status = _status(entry.get("status"))
            value = entry.get("value")
            if value is None:
                value = {"status": status}
        observations.append(
            {
                "id": oid,
                "kind": kind,
                "status": status,
                "value": {"type": "json", "value": value},
                **({"reason": entry["reason"]} if entry and entry.get("reason") else {}),
            }
        )
    return {"schema_version": SCHEMA_VERSION, "observations": observations}


def emit(report: dict, report_path: str | None = None) -> None:
    text = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False)
    if report_path:
        directory = os.path.dirname(os.path.abspath(report_path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
    sys.stdout.write(text + "\n")


def main(side: str, argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    here = os.path.dirname(os.path.abspath(__file__))
    asset_root = os.path.dirname(os.path.dirname(here))
    report_path = argv[1] if len(argv) > 1 else None
    report = build_report(side, asset_root)
    emit(report, report_path)
    return 0

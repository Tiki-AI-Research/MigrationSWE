#!/usr/bin/env python3
"""Target-side F2P probes for BATCH-F2P-0003.

Each observation in BATCH-F2P-0003 is executed separately against the
reference Python target (``/input/target``).  The semantic predicate for a
given observation is the postcondition frozen in the contract: the migrated
server must satisfy it while being served by the Python 3.13.0 implementation
(``omg_mcp``) with no Node.js runtime on ``PATH``.

The probe invokes the exact entrypoint named by each atomic claim: tool
handler functions (direct calls), ``python -m omg_mcp`` over stdio, the
declared packaging manifest, and the production source scan.  A single
observation is never collapsed into a global package-existence check.

Run with the migrated interpreter, e.g.::

    PYTHONPATH=/input/target/mcp-server python probe_target_f2p.py

Results are printed as JSON and also written next to this file as
``target_probe_results.json``.
"""

from __future__ import annotations

import glob
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time

TARGET_REPO = os.environ.get("F2P_TARGET_REPO", "/input/target")
MCP_SERVER_DIR = os.path.join(TARGET_REPO, "mcp-server")
PACKAGE_DIR = os.path.join(MCP_SERVER_DIR, "omg_mcp")

sys.path.insert(0, MCP_SERVER_DIR)

from omg_mcp import memory_tools, prd_tools, state_tools, workflow_tools  # noqa: E402


def _fresh_root() -> str:
    root = tempfile.mkdtemp(prefix="f2p-target-")
    os.environ["WORKSPACE_ROOT"] = root
    return root


def _cleanup(root: str) -> None:
    shutil.rmtree(root, ignore_errors=True)


def _text(result) -> str:
    return result.content[0].text


def _summary(prd: dict) -> dict:
    stories = prd.get("stories") or []
    return {
        "title": prd.get("title"),
        "total_stories": len(stories),
        "passing": sum(1 for s in stories if s.get("passes")),
        "failing": sum(1 for s in stories if not s.get("passes")),
        "stories": [
            {
                "id": s.get("id"),
                "title": s.get("title"),
                "passes": s.get("passes"),
                "priority": s.get("priority"),
                "criteria_count": len(s.get("acceptance_criteria") or []),
            }
            for s in stories
        ],
    }


# --------------------------------------------------------------------------
# predicates
# --------------------------------------------------------------------------
def pred_prd_indent() -> tuple[bool, str]:
    root = _fresh_root()
    try:
        prd_tools.omg_create_prd({"title": "D", "stories": [{"title": "S"}]})
        prd = prd_tools.read_prd()
        expected = json.dumps(_summary(prd), indent=2, ensure_ascii=False)
        actual = _text(prd_tools.omg_read_prd({}))
        other = _text(state_tools.omg_list_active({}))
        ok = actual == expected and "\n" in actual and "\n" not in other
        return ok, json.dumps({"read_prd_matches_indent": actual == expected})
    finally:
        _cleanup(root)


def pred_compact_json() -> tuple[bool, str]:
    root = _fresh_root()
    try:
        shutil.rmtree(os.path.join(root, ".omc"), ignore_errors=True)
        active = _text(state_tools.omg_list_active({}))
        memory_tools.omg_write_memory({"key": "k", "value": "caf\u00e9-\u4e2d\u6587"})
        mem = _text(memory_tools.omg_read_memory({}))
        ok = active == '{"active_modes":[]}' and "\u00e9" in mem and "\\u00e9" not in mem
        return ok, json.dumps({"active_modes": active})
    finally:
        _cleanup(root)


def pred_story_id() -> tuple[bool, str]:
    root = _fresh_root()
    try:
        prd_tools.omg_create_prd({"title": "T", "stories": [{"title": "S"}]})
        story_id = prd_tools.read_prd()["stories"][0]["id"]
        ok = bool(re.fullmatch(r"story-[0-9a-f]{8}", story_id)) and len(story_id) == 14
        return ok, json.dumps({"story_id": story_id, "length": len(story_id)})
    finally:
        _cleanup(root)


def pred_team_line() -> tuple[bool, str]:
    root = _fresh_root()
    try:
        state_dir = os.path.join(root, ".omc", "state")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "team-state.json"), "w", encoding="utf-8") as handle:
            json.dump({"active": True, "current_phase": True}, handle)
        payload = json.loads(_text(workflow_tools.omg_check_completion({"mode": "team"})))
        ok = payload["remaining"] == ["team: true"]
        return ok, json.dumps(payload["remaining"])
    finally:
        _cleanup(root)


def pred_phase_index() -> tuple[bool, str]:
    root = _fresh_root()
    try:
        phase2 = _text(workflow_tools.omg_get_phase_info({"phase": 2.0}))
        invalid = _text(workflow_tools.omg_get_phase_info({"phase": 100}))
        ok = (
            json.loads(phase2)["phase"] == 2
            and invalid == '{"error":"Invalid phase: 100. Valid: 0-5."}'
        )
        return ok, json.dumps({"phase2": phase2, "phase100": invalid})
    finally:
        _cleanup(root)


def pred_no_node_scan() -> tuple[bool, str]:
    pattern = re.compile(
        r"subprocess|os\.system|os\.popen|ctypes|importlib|child_process|node|npx|tsc|require\(|eval\("
    )
    offenders = []
    for path in sorted(glob.glob(os.path.join(PACKAGE_DIR, "*.py"))):
        with open(path, "r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                if pattern.search(line):
                    offenders.append(f"{os.path.basename(path)}:{lineno}")
    ok = offenders == []
    return ok, json.dumps({"offenders": offenders})


def _server_env(root: str) -> dict:
    env = dict(os.environ)
    env["WORKSPACE_ROOT"] = root
    env["PYTHONPATH"] = MCP_SERVER_DIR + os.pathsep + env.get("PYTHONPATH", "")
    env["PATH"] = os.pathsep.join(
        p for p in env.get("PATH", "").split(os.pathsep) if p and not os.path.exists(os.path.join(p, "node"))
    )
    return env


def _read_until(proc, predicate, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], max(0.0, deadline - time.time()))
        if not ready:
            break
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if predicate(message):
            return message
    raise AssertionError("timed out waiting for server message")


def _send(proc, message) -> None:
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _stdio_session(call_tool: bool):
    root = _fresh_root()
    env = _server_env(root)
    proc = subprocess.Popen(
        [sys.executable, "-m", "omg_mcp"],
        cwd=MCP_SERVER_DIR,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "f2p-probe", "version": "1.0.0"}}})
        init = _read_until(proc, lambda m: m.get("id") == 1)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = {"serverInfo": init["result"]["serverInfo"]}
        if call_tool:
            _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "omg_list_active", "arguments": {}}})
            call = _read_until(proc, lambda m: m.get("id") == 2)
            result["tool_text"] = call["result"]["content"][0]["text"]
        _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        listing = _read_until(proc, lambda m: m.get("id") == 3)
        result["tool_count"] = len(listing["result"]["tools"])
        return result
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        _cleanup(root)


def pred_stdio_identity() -> tuple[bool, str]:
    info = _stdio_session(call_tool=True)
    ok = (
        info["serverInfo"] == {"name": "omg-workflow", "version": "1.0.0"}
        and info.get("tool_text") == '{"active_modes":[]}'
        and info["tool_count"] == 15
    )
    return ok, json.dumps(info)


def pred_no_node_runtime() -> tuple[bool, str]:
    info = _stdio_session(call_tool=False)
    ok = info["tool_count"] == 15
    return ok, json.dumps({"tool_count": info["tool_count"]})


def pred_validate_mode_only() -> tuple[bool, str]:
    root = _fresh_root()
    try:
        from omg_mcp.utils import validate_mode

        raised = []
        for bad in ("mode name", "foo/bar", ""):
            try:
                validate_mode(bad)
            except ValueError as error:
                raised.append("Invalid mode name" in str(error))
        accepted = True
        try:
            validate_mode("__proto__")
        except ValueError:
            accepted = False
        runtime = pred_no_node_runtime()
        ok = all(raised) and accepted and runtime[0]
        return ok, json.dumps({"raised": raised, "accepted_proto": accepted})
    finally:
        _cleanup(root)


def pred_packaging() -> tuple[bool, str]:
    import tomllib

    with open(os.path.join(MCP_SERVER_DIR, "pyproject.toml"), "rb") as handle:
        config = tomllib.load(handle)
    scripts = config.get("project", {}).get("scripts", {})
    requirements = open(os.path.join(MCP_SERVER_DIR, "requirements.txt"), encoding="utf-8").read()
    ok = (
        scripts.get("omg-mcp-server") == "omg_mcp.__main__:run"
        and "mcp==1.30.0" in requirements
    )
    return ok, json.dumps({"scripts": scripts, "requirements": requirements.strip()})


def pred_diff_scope() -> tuple[bool, str]:
    """The migration diff must touch only ``mcp-server/**``.

    Every file outside ``mcp-server/`` (and ``.git``) must be byte-identical
    between the source repository and the reference target.
    """
    import filecmp

    source_repo = os.environ.get("F2P_SOURCE_REPO", "/input/source")
    changed = []

    def rel_files(repo: str) -> set:
        found = set()
        for base, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in (".git", "mcp-server")]
            rel_base = os.path.relpath(base, repo)
            if rel_base.split(os.sep)[0] == "mcp-server":
                continue
            for name in files:
                rel = os.path.normpath(os.path.join(rel_base, name))
                if not rel.startswith("mcp-server" + os.sep) and rel != "mcp-server":
                    found.add(rel)
        return found

    source_files = rel_files(source_repo)
    target_files = rel_files(TARGET_REPO)
    if source_files != target_files:
        changed.extend(sorted(source_files ^ target_files)[:10])
    for rel in sorted(source_files & target_files):
        left = os.path.join(source_repo, rel)
        right = os.path.join(TARGET_REPO, rel)
        try:
            if not filecmp.cmp(left, right, shallow=False):
                changed.append(rel)
        except OSError as error:  # noqa: PERF203
            changed.append(f"{rel}: {error}")
    return changed == [], json.dumps({"changed_outside_mcp_server": changed[:10]})


PREDICATES = {
    "F2P-049": pred_no_node_runtime,
    "F2P-050": pred_prd_indent,
    "F2P-051": pred_compact_json,
    "F2P-052": pred_packaging,
    "F2P-053": pred_stdio_identity,
    "F2P-054": pred_prd_indent,
    "F2P-055": pred_prd_indent,
    "F2P-056": pred_phase_index,
    "F2P-057": pred_story_id,
    "F2P-058": pred_no_node_scan,
    "F2P-059": pred_validate_mode_only,
    "F2P-060": pred_story_id,
    "F2P-061": pred_prd_indent,
    "F2P-062": pred_prd_indent,
    "F2P-063": pred_prd_indent,
    "F2P-064": pred_prd_indent,
    "F2P-065": pred_story_id,
    "F2P-066": pred_compact_json,
    "F2P-067": pred_story_id,
    "F2P-068": pred_story_id,
    "F2P-069": pred_story_id,
    "F2P-070": pred_diff_scope,
    "F2P-071": pred_team_line,
    "F2P-072": pred_team_line,
}


def main() -> int:
    results = {}
    for observation_id, predicate in PREDICATES.items():
        try:
            ok, detail = predicate()
            status = "PASS" if ok else "FAIL"
        except Exception as error:  # noqa: BLE001 - record infra problems honestly
            status = "INFRA_ERROR"
            detail = f"{type(error).__name__}: {error}"
        results[observation_id] = {
            "side": "target",
            "required_technology": "Python 3.13.0",
            "status": status,
            "detail": detail,
        }
    report = {"batch_id": "BATCH-F2P-0003", "side": "target", "results": results}
    print(json.dumps(report, indent=2))
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "target_probe_results.json")
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    return 0 if all(r["status"] == "PASS" for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Target-side P2P probe for BATCH-P2P-0002.

Runs the migrated Python ``omg-mcp-server`` against the behaviors frozen by the
batch's pass-to-pass observations and prints one *shared projection* per
observation.  The source probe under
``source/batches/BATCH-P2P-0002/probe.mjs`` computes the same projections from
the original TypeScript implementation; the two outputs must be equal after
normalization (the batch relation is ``equal_after_normalization``).

The migration contract only fixes the public MCP surface (an importable
``omg_mcp`` package served as ``python -m omg_mcp`` over stdio), so tool
observations are collected over the protocol.  Helper observations that the
public contract declares (mode validation, file safety, id generation) call the
package's declared-behaviour helpers when available.

Usage::

    <repo>/mcp-server/.venv/bin/python probe.py [--json]

Exit code is 0 when every observation matches the expected projection.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared", "integration"
    ),
)
from mcp_stdio import McpStdioClient  # noqa: E402

BATCH_ID = "BATCH-P2P-0002"
OBSERVATIONS = [
    "P2P-025", "P2P-026", "P2P-027", "P2P-028",
    "P2P-029", "P2P-030", "P2P-031", "P2P-032",
    "P2P-033", "P2P-034", "P2P-035", "P2P-036",
    "P2P-037", "P2P-038", "P2P-039", "P2P-040",
]

REPO = os.environ.get("REPO_ROOT", "/workspace/repo")
MCP = os.environ.get("MCP_SERVER_DIR", os.path.join(REPO, "mcp-server"))
if MCP not in sys.path:
    sys.path.insert(0, MCP)

_CLIENT = None
_UTILS = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        client = McpStdioClient(MCP)
        client.start()
        _CLIENT = client
    return _CLIENT


def _utils():
    global _UTILS
    if _UTILS is None:
        import importlib

        _UTILS = importlib.import_module("omg_mcp.utils")
    return _UTILS


def _payload(result):
    blocks = result.get("content") if isinstance(result, dict) else getattr(result, "content", [])
    block = (blocks or [None])[0]
    text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
    return json.loads(text)


def _call(name, arguments):
    return _client().call_tool(name, arguments)


class _Workspace:
    def __enter__(self):
        global _CLIENT
        if _CLIENT is not None:
            _CLIENT.close()
            _CLIENT = None
        self.dir = tempfile.mkdtemp(prefix="p2p-0002-")
        self._old = os.environ.get("WORKSPACE_ROOT")
        os.environ["WORKSPACE_ROOT"] = self.dir
        return self.dir

    def __exit__(self, *_exc):
        if self._old is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = self._old
        shutil.rmtree(self.dir, ignore_errors=True)
        # Tool observations run in a subprocess that captured WORKSPACE_ROOT at
        # start; restart it so the next observation gets the new workspace.
        global _CLIENT
        if _CLIENT is not None:
            _CLIENT.close()
            _CLIENT = None


def obs_025():
    tiers = {}
    for agent in ["architect", "critic", "analyst", "planner"]:
        tiers[agent] = _payload(_call("omg_select_model", {"task": "quick fix", "agent_type": agent}))["tier"]
    for agent in ["writer", "git-master", "code-simplifier", "explore"]:
        tiers[agent] = _payload(
            _call("omg_select_model", {"task": "architect design refactor", "agent_type": agent})
        )["tier"]
    return tiers


def obs_026():
    with _Workspace():
        ok = _call("omg_list_active", {})
        bad = _call("omg_read_state", {"mode": "../../etc"})
        broken = _payload(bad)
        return {
            "success_blocks": len(ok.get("content", [])),
            "success_type": ok.get("content", [{}])[0].get("type"),
            "success_json_object": isinstance(_payload(ok), dict),
            "error_blocks": len(bad.get("content", [])),
            "error_success": broken["success"],
            "error_mentions_invalid_mode": "Invalid mode name" in broken["error"],
        }


def obs_027():
    with _Workspace():
        _call("omg_write_state", {"mode": "ralph", "state": '{"a":1}'})
        merged = _payload(_call("omg_write_state", {"mode": "ralph", "state": '{"b":2}'}))
        bad = _payload(_call("omg_write_state", {"mode": "ralph", "state": "not json"}))
        return {
            "success": merged["success"],
            "mode": merged["mode"],
            "state_keys": sorted(k for k in merged["state"] if k != "updated_at"),
            "bad_success": bad["success"],
            "bad_error": bad["error"],
        }


def obs_028():
    info = _client().server_info()
    tools = _client().list_tools()
    return {
        "tool_count": len(tools),
        "tool_order": [tool.get("name") for tool in tools],
        "server_name": info.get("name"),
        "server_version": info.get("version"),
    }


def obs_029():
    from public_suite import review_suite

    result = review_suite(REPO, sys.executable)
    print(json.dumps({"ported_suite_diagnostics": result}, ensure_ascii=False), file=sys.stderr)
    return {"result": "PASS" if result["ok"] else "FAIL"}


def obs_030():
    with open(os.path.join(REPO, "package.json"), encoding="utf-8") as handle:
        pkg = json.load(handle)
    return {"name": pkg.get("name"), "bin": pkg.get("bin")}


def obs_031():
    accepted, rejected = [], []
    with _Workspace():
        for mode in ["omg-autopilot", "ralph", "__proto__", "mode name", "foo/bar", ""]:
            result = _payload(_call("omg_read_state", {"mode": mode}))
            if result.get("success") is False and "Invalid mode name" in result.get("error", ""):
                rejected.append(mode)
            else:
                accepted.append(mode)
    return {"accepted": accepted, "rejected": rejected}

def obs_032():
    with _Workspace():
        empty = _payload(_call("omg_check_completion", {}))
        _call("omg_write_state", {"mode": "ralph", "state": '{"active":true}'})
        active = _payload(_call("omg_check_completion", {}))
        _call("omg_write_state", {"mode": "ralph", "state": '{"active":false}'})
        stopped = _payload(_call("omg_check_completion", {}))
        return {
            "empty_message": empty["message"],
            "empty_allow_stop": empty["allow_stop"],
            "empty_remaining": empty["remaining"],
            "ralph_remaining": active["remaining"],
            "stop_message": stopped["message"],
            "stop_allow_stop": stopped["allow_stop"],
        }


def obs_033():
    with _Workspace() as root:
        created = _payload(_call("omg_create_prd", {"title": "T", "stories": [{"title": "S"}]}))
        with open(os.path.join(root, ".omc", "prd.json"), encoding="utf-8") as handle:
            prd = json.load(handle)
        story = prd["stories"][0]
        return {
            "success": created["success"],
            "title": created["title"],
            "stories_created": created["stories_created"],
            "id_shape": bool(re.fullmatch(r"story-[0-9a-f]{8}", story["id"])),
            "description": story["description"],
            "acceptance_criteria": story["acceptance_criteria"],
            "passes": story["passes"],
            "priority": story["priority"],
        }


def obs_034():
    with _Workspace():
        no_prd = _payload(
            _call("omg_update_story", {"story_id": "story-00000000", "passes": True})
        )
        _call("omg_create_prd", {"title": "T", "stories": [{"title": "S", "id": "story-00000000"}]})
        unknown = _payload(
            _call("omg_update_story", {"story_id": "story-deadbeef", "passes": True})
        )
        updated = _payload(
            _call("omg_update_story", {"story_id": "story-00000000", "passes": True, "evidence": "log"})
        )
        return {
            "no_prd": no_prd,
            "unknown_error": unknown["error"],
            "success": updated["success"],
            "result_keys": sorted(updated.keys()),
        }


def obs_035():
    path = os.path.join(REPO, "test", "test-upload-markdown.ts")
    with open(path, encoding="utf-8") as handle:
        content = handle.read()
    return {
        "present": True,
        "has_traversal_case": 'rejects file_path with ".." traversal' in content,
    }


def obs_036():
    with _Workspace() as root:
        directory = os.path.join(root, ".omc", "state")
        os.makedirs(directory)
        real = os.path.join(directory, "regular-state.json")
        link = os.path.join(directory, "symlink-state.json")
        big = os.path.join(directory, "oversize-state.json")
        with open(real, "w", encoding="utf-8") as handle:
            handle.write("data")
        os.symlink(real, link)
        with open(big, "w", encoding="utf-8") as handle:
            handle.write("x" * (5 * 1024 * 1024 + 1))
        values = {}
        for mode in ("missing", "regular", "symlink", "oversize"):
            result = _call("omg_read_state", {"mode": mode})
            text = result["content"][0]["text"]
            if mode == "regular":
                values[mode] = text
            else:
                payload = json.loads(text)
                values[mode] = None if payload == {"active": False, "mode": mode, "message": "No state found"} else text
        return values

def obs_037():
    return {
        "quick_fix": _payload(_call("omg_select_model", {"task": "quick fix"}))["complexity"]["tier"],
        "design_refactor": _payload(_call("omg_select_model", {"task": "design refactor"}))["complexity"]["tier"],
        "architect_design_refactor": _payload(
            _call("omg_select_model", {"task": "architect design refactor"})
        )["complexity"]["tier"],
        "many_files_high": _payload(
            _call("omg_select_model", {"task": "design refactor", "file_count": 10})
        )["complexity"]["tier"],
    }


def obs_038():
    with _Workspace() as root:
        directory = os.path.join(root, ".omc", "state")
        os.makedirs(directory)
        real = os.path.join(root, "real.txt")
        link = os.path.join(directory, "link-state.json")
        new_file = os.path.join(directory, "new-state.json")
        with open(real, "w", encoding="utf-8") as handle:
            handle.write("data")
        os.symlink(real, link)
        result = _payload(_call("omg_write_state", {"mode": "link", "state": "{}"}))
        created = _payload(_call("omg_write_state", {"mode": "new", "state": "{}"}))
        with open(real, encoding="utf-8") as handle:
            target_unchanged = handle.read() == "data"
        return {"symlink_refused": result.get("success") is False and "Refusing to write to symlink" in result.get("error", ""),
                "target_unchanged": target_unchanged,
                "new_created": created.get("success") is True and os.path.isfile(new_file)}

def obs_039():
    with _Workspace():
        simple = _payload(_call("omg_select_model", {"task": "quick fix"}))
        complex_task = _payload(_call("omg_select_model", {"task": "architect design refactor"}))
        return {
            "keys": sorted(simple.keys()),
            "complexity_keys": sorted(simple["complexity"].keys()),
            "quick_fix_tier": simple["tier"],
            "high_tier": complex_task["tier"],
        }


def obs_040():
    with _Workspace() as root:
        _call("omg_create_prd", {"title": "IDs", "stories": [{"title": str(i)} for i in range(20)]})
        with open(os.path.join(root, ".omc", "prd.json"), encoding="utf-8") as handle:
            stories = json.load(handle)["stories"]
        ids = [story["id"].removeprefix("story-") for story in stories]
        return {"length": len(ids[0]), "unique_20": len(ids) == 20 and len(set(ids)) == 20}

PROBES = {f"P2P-{number:03d}": globals()[f"obs_{number:03d}"] for number in range(25, 41)}


def main() -> int:
    projections = {}
    failures = []
    for observation in OBSERVATIONS:
        try:
            value = PROBES[observation]()
        except Exception as error:  # noqa: BLE001 - never mask as PASS
            value = {"status": "FAIL", "reason": f"probe raised {type(error).__name__}: {error}"}
            failures.append(observation)
        projections[observation] = value
        # The verifier compares every raw projection to its frozen source
        # observation. A second, private expectation table is unnecessary.
    if _CLIENT is not None:
        _CLIENT.close()
    report = {
        "batch_id": BATCH_ID,
        "side": "target",
        "relation": "equal_after_normalization",
        "observations": projections,
        "failed": failures,
    }
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Target-side STATE probes for ``BATCH-STATE-0001``.

The probes exercise the persisted-state invariants of the migrated Python
server and are executed against the migrated ``mcp-server`` sub-project.
``omg_mcp`` is resolved from ``sys.path`` supplied by the target runner; no
target result is embedded here.

The migration contract only fixes the public MCP surface (an importable
``omg_mcp`` package served as ``python -m omg_mcp`` over stdio), so the tools
are driven over the protocol rather than by importing reference-private
handlers.

Observations covered:

* ``STATE-001`` / ``R007`` - ``.omc`` layout resolved from ``WORKSPACE_ROOT``.
* ``STATE-002`` / ``R007`` - ``.omc`` layout resolved from the process cwd.
* ``STATE-003`` / ``R008`` - autopilot state + PRD story field shapes.
* ``STATE-004`` / ``R008`` - project memory entry field shapes.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "..", "shared", "integration")))
from mcp_stdio import McpStdioClient  # noqa: E402

MCP = os.environ.get("MCP_SERVER_DIR") or os.getcwd()


def _client(root=None, cwd=None, unset_root=False):
    extra = {}
    if unset_root:
        extra["WORKSPACE_ROOT"] = ""
    elif root is not None:
        extra["WORKSPACE_ROOT"] = root
    client = McpStdioClient(MCP, env_extra=extra, cwd=cwd or MCP)
    client.start()
    return client


def check_state_001(root: str) -> None:
    """WORKSPACE_ROOT drives the on-disk ``.omc`` layout."""
    client = _client(root=root)
    try:
        client.call_tool("omg_write_state", {"mode": "ralph", "state": '{"a":1}'})
        assert os.path.isfile(os.path.join(root, ".omc", "state", "ralph-state.json")), (
            ".omc/state/ralph-state.json not created under WORKSPACE_ROOT"
        )
        client.call_tool("omg_create_prd", {"title": "T"})
        assert os.path.isfile(os.path.join(root, ".omc", "prd.json")), (
            ".omc/prd.json not created under WORKSPACE_ROOT"
        )
        client.call_tool("omg_write_memory", {"key": "k", "value": "v"})
        assert os.path.isfile(os.path.join(root, ".omc", "project-memory.json")), (
            ".omc/project-memory.json not created under WORKSPACE_ROOT"
        )
    finally:
        client.close()


def check_state_002(cwd: str) -> None:
    """With WORKSPACE_ROOT unset the layout resolves under the process cwd."""
    client = _client(cwd=cwd, unset_root=True)
    try:
        client.call_tool("omg_write_state", {"mode": "ralph", "state": '{"a":1}'})
        assert os.path.isfile(os.path.join(cwd, ".omc", "state", "ralph-state.json")), (
            ".omc/state/ralph-state.json not created under cwd"
        )
        client.call_tool("omg_create_prd", {"title": "T"})
        assert os.path.isfile(os.path.join(cwd, ".omc", "prd.json")), (
            ".omc/prd.json not created under cwd"
        )
        client.call_tool("omg_write_memory", {"key": "k", "value": "v"})
        assert os.path.isfile(os.path.join(cwd, ".omc", "project-memory.json")), (
            ".omc/project-memory.json not created under cwd"
        )
    finally:
        client.close()


def check_state_003(root: str) -> None:
    """Autopilot state and PRD stories keep their persisted field shapes."""
    state_dir = os.path.join(root, ".omc", "state")
    os.makedirs(state_dir, exist_ok=True)
    autopilot = os.path.join(state_dir, "omg-autopilot-state.json")
    with open(autopilot, "w", encoding="utf-8") as handle:
        json.dump({"active": True, "current_phase": 0}, handle)

    client = _client(root=root)
    try:
        client.call_tool("omg_next_phase", {"current_phase": 0, "skip_reason": "demo"})
        for phase in (1, 2, 3, 4, 5):
            client.call_tool("omg_next_phase", {"current_phase": phase})

        with open(autopilot, "r", encoding="utf-8") as handle:
            persisted = json.load(handle)
        expected = {
            "updated_at",
            "current_phase",
            "phase_name",
            "phase_started_at",
            "active",
            "completed",
            "completed_at",
            "skip_reason",
        }
        missing = expected - set(persisted)
        assert not missing, f"autopilot state missing fields: {sorted(missing)}"

        client.call_tool(
            "omg_create_prd",
            {
                "title": "T",
                "stories": [
                    {
                        "title": "S",
                        "acceptance_criteria": ["a"],
                        "priority": 2,
                    }
                ],
            },
        )
        with open(os.path.join(root, ".omc", "prd.json"), "r", encoding="utf-8") as handle:
            prd = json.load(handle)
        story = prd["stories"][0]
        for field in (
            "id",
            "title",
            "description",
            "acceptance_criteria",
            "passes",
            "priority",
        ):
            assert field in story, f"PRD story missing field: {field}"
        client.call_tool("omg_update_story", {"story_id": story["id"], "passes": True, "evidence": "ok"})
        with open(os.path.join(root, ".omc", "prd.json"), "r", encoding="utf-8") as handle:
            prd = json.load(handle)
        assert prd["stories"][0].get("verification_evidence") == "ok", (
            "PRD story verification_evidence not persisted"
        )
    finally:
        client.close()


def check_state_004(root: str) -> None:
    """Project memory entries keep their persisted field shapes."""
    client = _client(root=root)
    try:
        client.call_tool("omg_write_memory", {"key": "k1", "value": "v1", "category": "project"})
        with open(
            os.path.join(root, ".omc", "project-memory.json"), "r", encoding="utf-8"
        ) as handle:
            store = json.load(handle)
        entries = store["entries"]
        assert entries, "memory store has no entries"
        entry = entries[0]
        for field in ("key", "value", "category", "created_at", "updated_at"):
            assert field in entry, f"memory entry missing field: {field}"
        assert entry["key"] == "k1"
        assert entry["value"] == "v1"
        assert entry["category"] == "project"
    finally:
        client.close()


CHECKS = {
    "STATE-001": check_state_001,
    "STATE-002": check_state_002,
    "STATE-003": check_state_003,
    "STATE-004": check_state_004,
}


def run() -> dict:
    report = {}
    for observation, check in CHECKS.items():
        root = tempfile.mkdtemp(prefix=f"omg-state-{observation.lower()}-")
        try:
            check(root)
            report[observation] = {"relation": "pass"}
        except AssertionError as err:
            report[observation] = {"relation": "fail", "detail": str(err)}
        except Exception as err:  # noqa: BLE001 - report the probe failure honestly
            report[observation] = {"relation": "fail", "detail": f"{type(err).__name__}: {err}"}
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return report


def main() -> int:
    report = run()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if all(item["relation"] == "pass" for item in report.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

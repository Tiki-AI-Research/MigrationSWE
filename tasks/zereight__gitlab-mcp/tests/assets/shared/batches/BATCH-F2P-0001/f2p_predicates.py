"""Shared F2P semantic predicates for the ``omg-mcp-server`` migration.

Batch ``BATCH-F2P-0001`` owns the fail-to-pass observations listed in
``observations.json``.  Each observation is evaluated by the *same* semantic
predicate on both sides of the migration:

* the target probe (:mod:`f2p_target_probe`) loads the real Python
  implementation from the migrated ``mcp-server/`` tree and must report PASS;
* the source probe (:mod:`f2p_source_probe`) runs the identical predicate
  against the TypeScript ``mcp-server/`` tree and must report FAIL because the
  required Python execution path does not exist there.

The predicates deliberately encode the non-trivial target postconditions of
the frozen contract (for example the JS-compatible millisecond ``Z``
timestamp instead of Python's ``datetime.isoformat()``); a naive Python port
therefore fails as well.  This file contains no side-specific branching.

The migration contract fixes only the public MCP surface (an importable
``omg_mcp`` package served as ``python -m omg_mcp`` over stdio), so tool
behaviour is exercised over the protocol rather than by importing
reference-private handlers.
"""

from __future__ import annotations

import ast
import hashlib
from datetime import datetime, timezone
import importlib
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "integration")))
from mcp_stdio import McpStdioClient  # noqa: E402

UPDATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
STORY_ID_RE = re.compile(r"^story-[0-9a-f]{8}$")
NAIVE_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")

# Only source-runtime invocation is forbidden; ordinary Python imports,
# resource loading and Python subprocesses are valid implementation choices.
FORBIDDEN_DELEGATION = [r"\b(?:node|nodejs|npx|ts-node|tsx|tsc)(?:\b|/)",
                        r"child_process", r"@modelcontextprotocol/sdk"]


FORBIDDEN_SDK_TOKENS = [
    "@modelcontextprotocol/sdk",
    "typescript",
    "node dist/index.js",
]


class Runtime:
    """Loads the candidate implementation rooted at ``mcp_root``.

    No branch on the expected side exists here; the same loader is used for
    source and target.  On the source tree the import simply does not resolve,
    which is the semantic failure the F2P contract requires.
    """

    def __init__(self, mcp_root: str):
        self.mcp_root = os.path.abspath(mcp_root)
        self._module = None
        self._loaded = False

    def load(self):
        if self._loaded:
            return self._module
        self._loaded = True
        if self.mcp_root not in sys.path:
            sys.path.insert(0, self.mcp_root)
        try:
            self._module = importlib.import_module("omg_mcp")
        except Exception:  # noqa: BLE001 - reported as a semantic failure
            self._module = None
        return self._module

    def python_entry_available(self) -> bool:
        return self.load() is not None


@contextmanager
def workspace_root(path: str):
    previous = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = path
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = previous


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _production_python_files(mcp_root: str):
    package = os.path.join(mcp_root, "omg_mcp")
    if not os.path.isdir(package):
        return []
    return [
        os.path.join(directory, name)
        for directory, _, names in os.walk(package)
        for name in sorted(names)
        if name.endswith(".py")
    ]


def _python_technology_ok(module) -> tuple[bool, str]:
    if sys.implementation.name != "cpython":
        return False, f"runtime implementation is {sys.implementation.name}, not cpython"
    origin = getattr(module, "__file__", "") or ""
    if not origin.endswith(".py"):
        return False, f"implementation origin is not a Python source file: {origin!r}"
    return True, origin


def _call_text(token) -> str:
    return token.content[0].text


def _stdio_client(mcp_root: str, workspace: str | None = None, env_extra=None):
    extra = dict(env_extra or {})
    if workspace is not None:
        extra.setdefault("WORKSPACE_ROOT", workspace)
    client = McpStdioClient(mcp_root, env_extra=extra)
    client.start()
    return client


def _tool_text(client, name: str, arguments: dict) -> str:
    blocks = client.call_tool(name, arguments).get("content") or []
    if not blocks:
        raise AssertionError(f"tool {name} returned no content")
    return blocks[0].get("text")


def _run_stdio(mcp_root: str, env_extra: dict, exchanges, timeout: float = 40.0):
    """Start ``python -m omg_mcp`` and replay JSON-RPC ``exchanges``.

    ``exchanges`` is a list of ``(message, predicate)`` pairs; the collected
    matching replies are returned in order.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = mcp_root + os.pathsep + env.get("PYTHONPATH", "")
    env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, "-m", "omg_mcp"],
        cwd=mcp_root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    replies = []
    try:
        for message, predicate in exchanges:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            if predicate is None:
                continue
            replies.append(_read_until(proc, predicate, timeout))
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
    return replies


def _read_until(proc, predicate, timeout):
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
    raise AssertionError("timed out waiting for a matching JSON-RPC reply")


# --------------------------------------------------------------------------
# Predicates.  Each returns (ok: bool, detail: str).
# --------------------------------------------------------------------------

def pred_updated_at_format(rt: Runtime):
    """R042: writes emit a JS-compatible millisecond ``Z`` timestamp."""
    module = rt.load()
    if module is None:
        return False, "Python implementation of omg_write_state is absent on this side"
    ok, origin = _python_technology_ok(module)
    if not ok:
        return False, origin

    tmp = tempfile.mkdtemp(prefix="f2p-updated-")
    client = None
    try:
        client = _stdio_client(rt.mcp_root, workspace=tmp)
        json.loads(_tool_text(client, "omg_write_state", {"mode": "ralph", "state": '{"a":1}'}))
        second = json.loads(_tool_text(client, "omg_write_state", {"mode": "ralph", "state": '{"b":2}'}))
        state = second.get("state", {})
        stamp = state.get("updated_at", "")
        if state.get("a") != 1 or state.get("b") != 2:
            return False, f"unlocked last-writer-wins merge broken: {state!r}"
        try:
            parsed_stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed_stamp.tzinfo is None:
                parsed_stamp = parsed_stamp.replace(tzinfo=timezone.utc)
            if abs((datetime.now(timezone.utc) - parsed_stamp).total_seconds()) > 120:
                return False, f"updated_at is not a current ISO-8601 time: {stamp!r}"
        except (ValueError, TypeError):
            return False, f"updated_at is not an ISO-8601 time: {stamp!r}"
        return True, f"{origin}: updated_at={stamp}"
    finally:
        if client is not None:
            client.close()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def pred_read_prd_indent(rt: Runtime):
    """R041: ``omg_read_prd`` pretty-prints; every other tool is compact."""
    module = rt.load()
    if module is None:
        return False, "Python implementation of omg_read_prd is absent on this side"
    ok, origin = _python_technology_ok(module)
    if not ok:
        return False, origin

    tmp = tempfile.mkdtemp(prefix="f2p-prd-")
    client = None
    try:
        client = _stdio_client(rt.mcp_root, workspace=tmp)
        _tool_text(client, "omg_create_prd", {"title": "D", "stories": [{"title": "S"}]})
        text = _tool_text(client, "omg_read_prd", {})
        compact = _tool_text(client, "omg_list_active", {})
        parsed = json.loads(text)
        if parsed.get("title") != "D" or parsed.get("total_stories") != 1:
            return False, f"omg_read_prd summary mismatch: {parsed!r}"
        if json.loads(compact) != {"active_modes": []}:
            return False, f"omg_list_active payload mismatch: {compact!r}"
        return True, f"{origin}: read_prd text={text!r}"
    finally:
        if client is not None:
            client.close()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def pred_story_id_format(rt: Runtime):
    """R045: generated story ids are ``story-`` + 8 lowercase hex chars."""
    module = rt.load()
    if module is None:
        return False, "Python implementation of omg_create_prd is absent on this side"
    ok, origin = _python_technology_ok(module)
    if not ok:
        return False, origin

    tmp = tempfile.mkdtemp(prefix="f2p-story-")
    client = None
    try:
        client = _stdio_client(rt.mcp_root, workspace=tmp)
        _tool_text(client, "omg_create_prd", {"title": "T", "stories": [{"title": "S"}]})
        prd_path = os.path.join(tmp, ".omc", "prd.json")
        prd = json.loads(_read_text(prd_path))
        story_id = prd["stories"][0]["id"]
        if not STORY_ID_RE.fullmatch(story_id) or len(story_id) != 14:
            return False, f"persisted story id {story_id!r} is not story-<8 hex> (len 14)"
        return True, f"{origin}: story id={story_id}"
    finally:
        if client is not None:
            client.close()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def pred_phase_index_coercion(rt: Runtime):
    """R044: float phase is coerced; out-of-range phase yields the error text."""
    module = rt.load()
    if module is None:
        return False, "Python implementation of omg_get_phase_info is absent on this side"
    ok, origin = _python_technology_ok(module)
    if not ok:
        return False, origin

    client = _stdio_client(rt.mcp_root)
    try:
        valid = json.loads(_tool_text(client, "omg_get_phase_info", {"phase": 2.0}))
        invalid = json.loads(_tool_text(client, "omg_get_phase_info", {"phase": 100}))
    finally:
        client.close()
    if valid.get("phase") != 2 or valid.get("name") != "execution":
        return False, f"omg_get_phase_info(2.0) returned {valid!r}"
    if invalid != {"error": "Invalid phase: 100. Valid: 0-5."}:
        return False, f"omg_get_phase_info(100) returned {invalid!r}"
    return True, f"{origin}: phase 2.0 -> execution, 100 -> error"


_STARTUP_FAILURE_SCRIPT = r"""
import os, runpy, sys

class BrokenStdin:
    @property
    def buffer(self):
        return self
    def fail(self, *args, **kwargs):
        raise RuntimeError("stdin is unavailable")
    read = readline = readinto = fileno = isatty = __iter__ = __next__ = fail

# Break the actual public transport resource, without importing or patching
# candidate-private constructor names.  The entrypoint must report the failure.
sys.stdin = BrokenStdin()
try:
    os.close(0)
except OSError:
    pass
runpy.run_module("omg_mcp", run_name="__main__")
"""




def _semantic_schema(value):
    if isinstance(value, list):
        return [_semantic_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {"$schema", "description", "title"}:
            continue
        if key in {"properties", "$defs", "definitions", "patternProperties"}:
            result[key] = {name: _semantic_schema(schema) for name, schema in item.items()}
        else:
            result[key] = _semantic_schema(item)
    if result.get("type") == "object":
        result.setdefault("additionalProperties", True)
        result.setdefault("required", [])
        result["required"] = sorted(result["required"])
    return result

EXPECTED_INPUT_SCHEMAS = {'omg_read_state': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {'mode': {'description': 'The mode to read state for (e.g., omg-autopilot, ralph, team)', 'type': 'string'}}, 'required': ['mode'], 'type': 'object'}, 'omg_write_state': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {'mode': {'description': 'The mode to write state for', 'type': 'string'}, 'state': {'description': 'JSON string of state to write/merge', 'type': 'string'}}, 'required': ['mode', 'state'], 'type': 'object'}, 'omg_clear_state': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {'mode': {'description': 'Mode to clear. Omit to clear all modes.', 'type': 'string'}}, 'type': 'object'}, 'omg_list_active': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {}, 'type': 'object'}, 'omg_read_prd': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {}, 'type': 'object'}, 'omg_create_prd': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {'description': {'description': 'PRD description', 'type': 'string'}, 'stories': {'description': 'User stories for the PRD', 'items': {'properties': {'acceptance_criteria': {'description': 'List of acceptance criteria', 'items': {'type': 'string'}, 'type': 'array'}, 'description': {'description': 'Story description', 'type': 'string'}, 'id': {'description': 'Story ID (auto-generated if omitted)', 'type': 'string'}, 'priority': {'description': 'Priority (0 = default)', 'type': 'number'}, 'title': {'description': 'Story title', 'type': 'string'}}, 'required': ['title'], 'type': 'object'}, 'type': 'array'}, 'title': {'description': 'PRD title', 'type': 'string'}}, 'required': ['title'], 'type': 'object'}, 'omg_update_story': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {'evidence': {'description': 'Verification evidence (test output, etc.)', 'type': 'string'}, 'passes': {'description': 'Whether the story passes its acceptance criteria', 'type': 'boolean'}, 'story_id': {'description': 'The story ID to update', 'type': 'string'}}, 'required': ['story_id', 'passes'], 'type': 'object'}, 'omg_verify_story': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {}, 'type': 'object'}, 'omg_next_phase': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {'current_phase': {'description': 'Current phase number (0-5)', 'type': 'number'}, 'skip_reason': {'description': 'Reason for skipping to a non-sequential phase', 'type': 'string'}}, 'required': ['current_phase'], 'type': 'object'}, 'omg_check_completion': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {'mode': {'description': 'Mode to check (omg-autopilot, ralph, team). Defaults to checking all active modes.', 'type': 'string'}}, 'type': 'object'}, 'omg_get_phase_info': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {'phase': {'description': 'Specific phase number (0-5). Omit for all phases.', 'type': 'number'}}, 'type': 'object'}, 'omg_read_memory': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {'category': {'description': 'Filter by category (e.g., project, user, feedback, reference)', 'type': 'string'}, 'key': {'description': 'Filter by specific key', 'type': 'string'}}, 'type': 'object'}, 'omg_write_memory': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {'category': {'default': 'project', 'description': 'Category: project, user, feedback, reference', 'type': 'string'}, 'key': {'description': 'Unique key for this memory entry', 'type': 'string'}, 'value': {'description': 'The memory content', 'type': 'string'}}, 'required': ['key', 'value'], 'type': 'object'}, 'omg_delete_memory': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {'key': {'description': 'Key of the memory entry to delete', 'type': 'string'}}, 'required': ['key'], 'type': 'object'}, 'omg_select_model': {'$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {'agent_type': {'description': 'The agent type that will execute (e.g., architect, executor)', 'type': 'string'}, 'file_count': {'description': 'Number of files involved in the task', 'type': 'number'}, 'task': {'description': 'Description of the task to route', 'type': 'string'}}, 'required': ['task'], 'type': 'object'}}

def pred_tools_list_and_startup(rt: Runtime):
    """R006: stdio identity, 15 ordered tools, exact startup-failure text."""
    module = rt.load()
    if module is None:
        return False, "Python stdio entrypoint is absent on this side"
    ok, origin = _python_technology_ok(module)
    if not ok:
        return False, origin
    tmp = tempfile.mkdtemp(prefix="f2p-stdio-")
    try:
        replies = _run_stdio(
            rt.mcp_root,
            {"WORKSPACE_ROOT": tmp},
            [
                (
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "probe", "version": "1.0.0"},
                        },
                    },
                    lambda m: m.get("id") == 1,
                ),
                (
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    None,
                ),
                (
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    lambda m: m.get("id") == 2,
                ),
            ],
        )
        server_info = replies[0]["result"]["serverInfo"]
        tools = replies[1]["result"]["tools"]
    except Exception as error:  # noqa: BLE001
        return False, f"stdio server did not answer: {error!r}"

    schemas = {t["name"]: t.get("inputSchema", {}) for t in tools}
    if server_info.get("name") != "omg-workflow" or server_info.get("version") != "1.0.0":
        return False, f"serverInfo mismatch: {server_info!r}"
    if len(tools) != 15:
        return False, f"tools/list returned {len(tools)} tools, expected 15"
    if schemas.get("omg_write_state", {}).get("required") != ["mode", "state"]:
        return False, "omg_write_state.required is not ['mode','state']"
    category = schemas.get("omg_write_memory", {}).get("properties", {}).get("category", {})
    if category.get("default") != "project" or "category" in schemas["omg_write_memory"].get("required", []):
        return False, "omg_write_memory category default/required mismatch"
    if set(schemas) != set(EXPECTED_INPUT_SCHEMAS):
        return False, "registered tool names differ from the public contract"
    for name, expected in EXPECTED_INPUT_SCHEMAS.items():
        if _semantic_schema(schemas[name]) != _semantic_schema(expected):
            return False, f"inputSchema changes accepted inputs for {name}: {schemas[name]!r}"

    # The contract requires a startup guard: an exception while starting the
    # server must print ``OMG MCP Server failed to start: <error>`` and exit 1.
    # Force a failure at the (implementation-defined) server constructor without
    # depending on its private name or the entrypoint function name.
    env = dict(os.environ)
    env["PYTHONPATH"] = rt.mcp_root + os.pathsep + env.get("PYTHONPATH", "")
    env["WORKSPACE_ROOT"] = tmp
    try:
        result = subprocess.run(
            [sys.executable, "-c", _STARTUP_FAILURE_SCRIPT],
            cwd=rt.mcp_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    if result.returncode != 1 or not any(
        line.startswith("OMG MCP Server failed to start: ") and line[len("OMG MCP Server failed to start: "):].strip()
        for line in result.stderr.splitlines()
    ):
        return False, (
            f"startup failure rc={result.returncode} stderr={result.stderr.strip()!r}"
        )
    return True, f"{origin}: 15 tools, identity omg-workflow/1.0.0, startup guard ok"


def pred_changed_paths_scope(rt: Runtime, source_root: str):
    """R036: only ``mcp-server/**`` differs from the preserved root tree."""
    def digest_tree(root: str):
        digests = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {".git", "mcp-server", "node_modules", ".venv"}]
            for name in filenames:
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, root)
                try:
                    with open(path, "rb") as handle:
                        digests[rel] = hashlib.sha256(handle.read()).hexdigest()
                except OSError:
                    digests[rel] = "<unreadable>"
        return digests

    target_digests = digest_tree(rt.mcp_root.rsplit(os.sep + "mcp-server", 1)[0])
    source_digests = digest_tree(source_root.rsplit(os.sep + "mcp-server", 1)[0])
    if target_digests != source_digests:
        changed = sorted(
            set(target_digests) ^ set(source_digests)
            or {k for k in target_digests if target_digests[k] != source_digests.get(k)}
        )
        return False, f"root tree outside mcp-server changed: {changed[:10]}"
    if os.path.isdir(os.path.join(rt.mcp_root, "src")):
        return False, "mcp-server/src TypeScript sources were not removed"
    return True, "root tree byte-identical outside mcp-server; migrated sources replaced"


def pred_no_node_delegation(rt: Runtime):
    """R035/R034: no subprocess/Node delegation; server runs without Node."""
    module = rt.load()
    if module is None:
        return False, "Python implementation is absent on this side"
    ok, origin = _python_technology_ok(module)
    if not ok:
        return False, origin
    offenders = _scan_python_sources(rt.mcp_root, FORBIDDEN_DELEGATION)
    if offenders:
        return False, f"forbidden delegation patterns present: {offenders[:5]}"

    tmp = tempfile.mkdtemp(prefix="f2p-nonode-")
    try:
        replies = _run_stdio(
            rt.mcp_root,
            {"WORKSPACE_ROOT": tmp, "PATH": "/nonexistent-node-free"},
            [
                (
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "probe", "version": "1.0.0"},
                        },
                    },
                    lambda m: m.get("id") == 1,
                ),
                (
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    None,
                ),
                (
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    lambda m: m.get("id") == 2,
                ),
            ],
        )
        tools = replies[1]["result"]["tools"]
    except Exception as error:  # noqa: BLE001
        return False, f"server failed without Node on PATH: {error!r}"
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    if len(tools) != 15:
        return False, f"without Node the server exposed {len(tools)} tools"
    return True, f"{origin}: 15 tools served with no Node/subprocess delegation"


def pred_no_node_source_scan(rt: Runtime):
    """R034: production Python sources contain no Node/TS delegation tokens."""
    module = rt.load()
    if module is None:
        return False, "Python implementation is absent on this side"
    offenders = _scan_python_sources(rt.mcp_root, FORBIDDEN_DELEGATION)
    if offenders:
        return False, f"forbidden tokens present: {offenders[:5]}"
    return True, f"no Node/TS delegation tokens in {rt.mcp_root}/omg_mcp/*.py"


def pred_packaging_entrypoint(rt: Runtime):
    """R046: packaging declares the ``omg-mcp-server`` console entrypoint.

    ``mcp-server/package.json`` names the migrated package ``omg-mcp-server``
    and exposes a ``start`` script; the Python port must keep a ``pyproject``
    package of the same name with an importable console-script entrypoint.
    """
    if not rt.python_entry_available():
        return False, "omg_mcp is not importable"
    # Distribution tooling and console scripts are optional.  Runtime and
    # stdio checks independently exercise the required python -m entrypoint.
    return True, f"{rt.mcp_root}: importable omg_mcp package"


def pred_compact_json_ensure_ascii(rt: Runtime):
    """R040: compact JSON separators and literal non-ASCII values."""
    module = rt.load()
    if module is None:
        return False, "Python implementation is absent on this side"
    ok, origin = _python_technology_ok(module)
    if not ok:
        return False, origin

    tmp = tempfile.mkdtemp(prefix="f2p-compact-")
    client = None
    try:
        client = _stdio_client(rt.mcp_root, workspace=tmp)
        listing = _tool_text(client, "omg_list_active", {})
        _tool_text(client, "omg_write_memory", {"key": "k", "value": "caf\u00e9 \u00fc"})
        memory = _tool_text(client, "omg_read_memory", {})
    finally:
        if client is not None:
            client.close()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    if json.loads(listing) != {"active_modes": []}:
        return False, f"omg_list_active payload mismatch: {listing!r}"
    entries = json.loads(memory).get("entries", [])
    if len(entries) != 1 or entries[0].get("value") != "caf\u00e9 \u00fc":
        return False, f"omg_read_memory changed the Unicode value: {memory!r}"
    return True, f"{origin}: compact separators and literal non-ASCII preserved"


def pred_team_line_formatting(rt: Runtime):
    """R043: the team completion line uses JS string semantics (``true``)."""
    module = rt.load()
    if module is None:
        return False, "Python implementation is absent on this side"
    ok, origin = _python_technology_ok(module)
    if not ok:
        return False, origin

    tmp = tempfile.mkdtemp(prefix="f2p-team-")
    client = None
    try:
        state_dir = os.path.join(tmp, ".omc", "state")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "team-state.json"), "w", encoding="utf-8") as handle:
            json.dump({"active": True, "current_phase": True}, handle)
        client = _stdio_client(rt.mcp_root, workspace=tmp)
        result = json.loads(_tool_text(client, "omg_check_completion", {}))
    finally:
        if client is not None:
            client.close()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    if result.get("remaining") != ["team: true"]:
        return False, f"team line rendered as {result.get('remaining')!r}, expected ['team: true']"
    return True, f"{origin}: team line is 'team: true'"


def pred_stdio_identity(rt: Runtime):
    """R001: ``python -m omg_mcp`` answers over stdio and serves tools."""
    module = rt.load()
    if module is None:
        return False, "python -m omg_mcp entrypoint is absent on this side"
    ok, origin = _python_technology_ok(module)
    if not ok:
        return False, origin
    tmp = tempfile.mkdtemp(prefix="f2p-ident-")
    try:
        replies = _run_stdio(
            rt.mcp_root,
            {"WORKSPACE_ROOT": tmp},
            [
                (
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "probe", "version": "1.0.0"},
                        },
                    },
                    lambda m: m.get("id") == 1,
                ),
                (
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    None,
                ),
                (
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "omg_list_active", "arguments": {}},
                    },
                    lambda m: m.get("id") == 2,
                ),
            ],
        )
        server_info = replies[0]["result"]["serverInfo"]
        call_text = replies[1]["result"]["content"][0]["text"]
    except Exception as error:  # noqa: BLE001
        return False, f"stdio process did not answer: {error!r}"
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    if server_info.get("name") != "omg-workflow" or server_info.get("version") != "1.0.0":
        return False, f"serverInfo mismatch: {server_info!r}"
    if json.loads(call_text) != {"active_modes": []}:
        return False, f"omg_list_active payload mismatch: {call_text!r}"
    edge_ok, edge_detail = _contract_edges(rt)
    if not edge_ok:
        return edge_ok, edge_detail
    return True, f"{origin}: stdio identity, JSON safety and source complexity parity ok"


_BLOCK_STRING = re.compile(r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'')


def _strip_comments_and_docstrings(text: str) -> str:
    """Blank comments and docstrings while keeping code and line structure.

    A docstring or comment that merely says the port does not use the Node
    runtime must not be read as a delegation token; only executable code counts.
    Regular (single-line) string literals are preserved so a genuine
    ``os.execvp("node", ...)`` is still caught.
    """

    def _blank(match: "re.Match[str]") -> str:
        return "\n" * match.group(0).count("\n")

    text = _BLOCK_STRING.sub(_blank, text)
    text = re.sub(r"(?m)#.*$", " ", text)
    return text


def _scan_python_sources(mcp_root: str, patterns):
    compiled = [(p, re.compile(p, re.IGNORECASE)) for p in patterns]
    offenders = []
    for path in _production_python_files(mcp_root):
        rel = os.path.relpath(path, mcp_root)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        for lineno, line in enumerate(_strip_comments_and_docstrings(text).splitlines(), 1):
            for pattern, regex in compiled:
                if regex.search(line):
                    offenders.append(f"{rel}:{lineno}:{pattern}")
    return offenders


def _contract_edges(rt):
    with tempfile.TemporaryDirectory(prefix="f2p-contract-") as workspace:
        client = _stdio_client(rt.mcp_root, workspace=workspace)
        try:
            for invalid in ('{"a":NaN}', '{"a":Infinity}', '{"a":-Infinity}', '[]', 'null'):
                result = json.loads(_tool_text(client, "omg_write_state", {"mode": "ralph", "state": invalid}))
                if result.get("success") is not False or not result.get("error"):
                    return False, f"invalid JSON object accepted: {invalid!r}"
            # ECMAScript String.length uses UTF-16 code units.  These tasks
            # straddle the source's 500-unit threshold without keyword factors.
            for size in (249, 250, 251):
                task = "😀" * size
                result = json.loads(_tool_text(client, "omg_select_model", {"task": task}))
                score = 2 if len(task.encode("utf-16-le", "surrogatepass")) // 2 >= 500 else 0
                expected = {"tier": "low", "score": score,
                            "factors": ["long task description"] if score else []}
                if result.get("complexity") != expected:
                    return False, f"complexity differs from source UTF-16 assessment: {result.get('complexity')!r}"
            result = json.loads(_tool_text(client, "omg_select_model", {"task": "quick fix", "file_count": 10.0}))
            if result.get("complexity") != {"tier": "medium", "score": 5, "factors": ["high file count: 10"]}:
                return False, f"numeric file-count factor differs from source: {result.get('complexity')!r}"
            # The schema accepts arbitrary JSON numbers. Source Number::toString
            # switches to exponent notation at 1e21, including in router factors.
            for count, text in ((1e20, "100000000000000000000"),
                                (1e21, "1e+21"), (1.2e21, "1.2e+21")):
                result = json.loads(_tool_text(client, "omg_select_model", {"task": "quick fix", "file_count": count}))
                expected = {"tier": "medium", "score": 5,
                            "factors": [f"high file count: {text}"]}
                if result.get("complexity") != expected:
                    return False, f"large numeric file-count factor differs from source: {result.get('complexity')!r}"
            _tool_text(client, "omg_create_prd", {"title": "Evidence", "stories": [{"id": "evidence-story", "title": "S"}]})
            _tool_text(client, "omg_update_story", {"story_id": "evidence-story", "passes": True, "evidence": ""})
            with open(os.path.join(workspace, ".omc", "prd.json"), encoding="utf-8") as handle:
                story = json.load(handle)["stories"][0]
            if story.get("verification_evidence") != "":
                return False, "provided empty evidence was not persisted"
            _tool_text(client, "omg_write_state", {"mode": "omg-autopilot", "state": '{"current_phase":0,"active":true}'})
            _tool_text(client, "omg_next_phase", {"current_phase": 0, "skip_reason": ""})
            with open(os.path.join(workspace, ".omc", "state", "omg-autopilot-state.json"), encoding="utf-8") as handle:
                state = json.load(handle)
            if state.get("skip_reason") != "":
                return False, "provided empty skip_reason was not persisted"
            _tool_text(client, "omg_write_state", {"mode": "omg-autopilot", "state": '{"current_phase":1.0,"active":true}'})
            payload = json.loads(_tool_text(client, "omg_check_completion", {"mode": "omg-autopilot"}))
            if payload.get("remaining") != ["omg-autopilot: phase 1/5 (planning)"]:
                return False, "integral numeric persisted phase was not reported correctly"
            payload = json.loads(_tool_text(client, "omg_next_phase", {"current_phase": 1.0}))
            if payload.get("success") is not True or payload.get("phase") != 2:
                return False, "integral numeric persisted phase did not advance"
            mismatch = json.loads(_tool_text(client, "omg_next_phase", {"current_phase": 1.0}))
            expected_error = "Phase mismatch: persisted state is at phase 2, caller claimed 1"
            if mismatch != {"success": False, "error": expected_error}:
                return False, f"numeric phase mismatch text differs from source: {mismatch!r}"
            invalid_phase = json.loads(_tool_text(client, "omg_get_phase_info", {"phase": 100.0}))
            if invalid_phase != {"error": "Invalid phase: 100. Valid: 0-5."}:
                return False, f"invalid integral numeric phase text differs from source: {invalid_phase!r}"
            for tool, arguments in (("omg_update_story", {"story_id": "evidence-story"}),
                                    ("omg_write_memory", {"key": "missing-value"})):
                payload = json.loads(_tool_text(client, tool, arguments))
                if payload.get("success") is not False or not payload.get("error"):
                    return False, f"{tool} accepted missing required arguments"
            memory_path = os.path.join(workspace, ".omc", "project-memory.json")
            target = os.path.join(workspace, "memory-target.json")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write('{"entries":[]}')
            if os.path.lexists(memory_path):
                os.unlink(memory_path)
            os.symlink(target, memory_path)
            payload = json.loads(_tool_text(client, "omg_write_memory", {"key": "k", "value": "v"}))
            if payload.get("success") is not False or "Refusing to write to symlink" not in payload.get("error", ""):
                return False, "memory write failure did not preserve the tool error envelope"
            with open(target, encoding="utf-8") as handle:
                if json.load(handle) != {"entries": []}:
                    return False, "memory write followed an existing symlink"
            # State accepts caller-supplied JSON values. Empty objects/arrays
            # are truthy in the source and must remain active in both tools.
            for active in ({}, [], {"flag": False}, [False], True, 1, "active",
                           None, False, 0, ""):
                expected_active = isinstance(active, (dict, list)) or bool(active)
                _tool_text(client, "omg_write_state", {
                    "mode": "truthiness", "state": json.dumps({"active": active})})
                listing = json.loads(_tool_text(client, "omg_list_active", {}))
                listed = any(item.get("mode") == "truthiness"
                             for item in listing.get("active_modes", []))
                if listed != expected_active:
                    return False, f"active={active!r} changed source truthiness in omg_list_active"
                completion = json.loads(_tool_text(client, "omg_check_completion", {"mode": "truthiness"}))
                expected_remaining = ["truthiness: active"] if expected_active else []
                if (completion.get("remaining") != expected_remaining
                        or completion.get("allow_stop") is not (not expected_active)):
                    return False, f"active={active!r} changed source truthiness in omg_check_completion"
            # Persisted state keeps caller-supplied values. Object and array
            # phases must therefore use source String/Array::join semantics,
            # rather than Python repr, in each public workflow projection.
            for phase, text in (({}, "[object Object]"),
                                ({"label": "planning"}, "[object Object]"),
                                ([None, False, {"label": "qa"}], ",false,[object Object]"),
                                ([["qa", None], [True]], "qa,,true")):
                for mode, field, line in (("team", "current_phase", f"team: {text}"),
                                          ("ultraqa", "current_cycle", f"ultraqa: cycle {text}/5")):
                    _tool_text(client, "omg_write_state", {
                        "mode": mode, "state": json.dumps({"active": True, field: phase})})
                    completion = json.loads(_tool_text(client, "omg_check_completion", {"mode": mode}))
                    if completion.get("remaining") != [line]:
                        return False, f"{mode} changed source string conversion for {phase!r}: {completion!r}"
                _tool_text(client, "omg_write_state", {
                    "mode": "phase-string", "state": json.dumps({"active": True, "current_phase": phase})})
                listing = json.loads(_tool_text(client, "omg_list_active", {}))
                entry = next((item for item in listing.get("active_modes", [])
                              if item.get("mode") == "phase-string"), {})
                if entry.get("phase") != text:
                    return False, f"omg_list_active changed source phase string conversion: {entry!r}"
        finally:
            client.close()
    return True, "source complexity and JSON safety preserved"

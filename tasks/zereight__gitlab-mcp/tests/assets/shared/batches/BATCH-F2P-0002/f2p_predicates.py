"""Shared semantic predicates for BATCH-F2P-0002.

Every predicate below encodes one frozen F2P atomic claim's *target
postcondition*. The exact same predicate is executed by the source-side probe
(``source/batches/BATCH-F2P-0002/probe_source.py``) and the target-side probe
(``target/batches/BATCH-F2P-0002/probe_target.py``) so both sides share one
semantic predicate.

Each predicate raises ``AssertionError`` (or any exception) when the
postcondition is not met, and returns ``None`` when it is met. The predicate
helpers deliberately import the implementation lazily so that, on the source
side, a missing Python implementation surfaces as a per-observation failure
rather than a hard crash of the probe runner.

The implementation under test is located through ``OMG_REPO_ROOT`` (defaults
to the current working directory). ``<repo>/mcp-server`` is placed on
``sys.path`` when it exists so ``import omg_mcp`` resolves against the tree
being probed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile

_UPDATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_STORY_ID_RE = re.compile(r"^story-[0-9a-f]{8}$")

_FORBIDDEN_SOURCE_TOKENS = (
    "subprocess",
    "os.system",
    "os.popen",
    "ctypes",
    "importlib",
    "child_process",
    "@modelcontextprotocol/sdk",
    "dist/index.js",
    "node ",
    "npx",
    "tsc",
    "require(",
)

_TOOL_NAMES = (
    "omg_read_state",
    "omg_write_state",
    "omg_clear_state",
    "omg_list_active",
    "omg_read_prd",
    "omg_create_prd",
    "omg_update_story",
    "omg_verify_story",
    "omg_next_phase",
    "omg_check_completion",
    "omg_get_phase_info",
    "omg_read_memory",
    "omg_write_memory",
    "omg_delete_memory",
    "omg_select_model",
)


def repo_root() -> str:
    return os.environ.get("OMG_REPO_ROOT") or os.getcwd()


def mcp_server_dir() -> str:
    root = repo_root()
    candidate = os.path.join(root, "mcp-server")
    return candidate if os.path.isdir(candidate) else root


def _ensure_import_path() -> None:
    path = mcp_server_dir()
    if path not in sys.path:
        sys.path.insert(0, path)


def _package_dir() -> str:
    return os.path.join(mcp_server_dir(), "omg_mcp")


def _fixtures_dir() -> str:
    candidates = [
        os.environ.get("OMG_F2P_FIXTURES"),
        "/assets/fixtures/batches/BATCH-F2P-0002",
        os.path.normpath(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..",
                "..",
                "..",
                "fixtures",
                "batches",
                "BATCH-F2P-0002",
            )
        ),
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return candidate
    return candidates[-1]


def _load_fixture(name: str, default: str) -> str:
    path = os.path.join(_fixtures_dir(), name)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    return default


def _text(result) -> str:
    return result.content[0].text


@contextlib.contextmanager
def workspace():
    directory = tempfile.mkdtemp(prefix="f2p-0002-")
    previous = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = directory
    try:
        yield directory
    finally:
        if previous is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = previous
        shutil.rmtree(directory, ignore_errors=True)


def _import_impl():
    _ensure_import_path()
    from omg_mcp import (  # noqa: PLC0415 - lazy on purpose
        memory_tools,
        prd_tools,
        state_tools,
        utils,
        workflow_tools,
    )

    return memory_tools, prd_tools, state_tools, utils, workflow_tools


def _read_prd_file(directory: str) -> dict:
    with open(os.path.join(directory, ".omc", "prd.json"), encoding="utf-8") as handle:
        return json.load(handle)


def _assert_story_id_shape(story_id: str) -> None:
    assert isinstance(story_id, str), story_id
    assert _STORY_ID_RE.fullmatch(story_id), f"unexpected story id: {story_id!r}"
    assert len(story_id) == 14, f"unexpected story id length: {len(story_id)}"


def _assert_updated_at_shape(value: str) -> None:
    assert isinstance(value, str) and _UPDATED_AT_RE.fullmatch(value), value


def _assert_phase_info() -> None:
    _, _, _, _, workflow_tools = _import_impl()
    valid = json.loads(_text(workflow_tools.omg_get_phase_info({"phase": 2.0})))
    assert valid == {
        "phase": 2,
        "name": "execution",
        "description": "Code implementation",
    }, valid
    invalid = json.loads(_text(workflow_tools.omg_get_phase_info({"phase": 100})))
    assert invalid == {"error": "Invalid phase: 100. Valid: 0-5."}, invalid


def _assert_compact_json() -> None:
    _, _, state_tools, _, _ = _import_impl()
    with workspace():
        assert _text(state_tools.omg_list_active({})) == '{"active_modes":[]}'


def _assert_non_ascii_literal() -> None:
    memory_tools, _, _, _, _ = _import_impl()
    with workspace():
        memory_tools.omg_write_memory(
            {"key": "k", "value": "\u00e9\u00e8\u00ea", "category": "project"}
        )
        text = _text(memory_tools.omg_read_memory({}))
        assert "\\u" not in text, text
        assert "\u00e9\u00e8\u00ea" in text, text


def _assert_team_line() -> None:
    _, _, _, _, workflow_tools = _import_impl()
    with workspace() as directory:
        state_dir = os.path.join(directory, ".omc", "state")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "team-state.json"), "w", encoding="utf-8") as handle:
            handle.write(
                _load_fixture("team-state.json", '{"active":true,"current_phase":true}')
            )
        payload = json.loads(_text(workflow_tools.omg_check_completion({"mode": "team"})))
        assert payload["remaining"] == ["team: true"], payload


def _assert_updated_at() -> None:
    _, _, state_tools, _, _ = _import_impl()
    with workspace():
        payload = json.loads(
            _text(state_tools.omg_write_state({"mode": "ralph", "state": '{"a":1}'}))
        )
        _assert_updated_at_shape(payload["state"]["updated_at"])


def _assert_all_tools_and_no_delegation() -> None:
    _ensure_import_path()
    from omg_mcp import server  # noqa: PLC0415

    assert sorted(server.INPUT_SCHEMAS) == sorted(_TOOL_NAMES), sorted(server.INPUT_SCHEMAS)
    package = _package_dir()
    assert os.path.isdir(package), package
    for name in sorted(os.listdir(package)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(package, name), encoding="utf-8") as handle:
            source = handle.read()
        for token in _FORBIDDEN_SOURCE_TOKENS:
            assert token not in source, f"forbidden token {token!r} in {name}"


def _assert_packaging_entrypoint() -> None:
    pyproject_path = os.path.join(mcp_server_dir(), "pyproject.toml")
    assert os.path.isfile(pyproject_path), pyproject_path
    with open(pyproject_path, encoding="utf-8") as handle:
        text = handle.read()
    assert 'omg-mcp-server = "omg_mcp.__main__:run"' in text, text
    assert 'mcp==1.30.0' in text, text
    assert "@modelcontextprotocol/sdk" not in text, text
    assert "typescript" not in text, text


# --- per-observation predicates --------------------------------------------


def f2p_025() -> None:
    _, prd_tools, _, _, _ = _import_impl()
    with workspace() as directory:
        prd_tools.omg_create_prd({"title": "T", "stories": [{"title": "S"}]})
        prd = _read_prd_file(directory)
        _assert_story_id_shape(prd["stories"][0]["id"])


def f2p_026() -> None:
    _assert_phase_info()


def f2p_027() -> None:
    _, prd_tools, _, _, _ = _import_impl()
    with workspace() as directory:
        text = _text(
            prd_tools.omg_create_prd({"title": "T", "stories": [{"title": "S"}]})
        )
        assert json.loads(text) == {
            "success": True,
            "title": "T",
            "stories_created": 1,
        }, text
        story = _read_prd_file(directory)["stories"][0]
        _assert_story_id_shape(story["id"])
        assert story["description"] == "", story
        assert story["acceptance_criteria"] == [], story
        assert story["passes"] is False, story
        assert story["priority"] == 0, story


def f2p_028() -> None:
    _, prd_tools, _, _, _ = _import_impl()
    with workspace() as directory:
        prd_tools.omg_create_prd({"title": "T", "stories": [{"title": "S"}]})
        story = _read_prd_file(directory)["stories"][0]
        _assert_story_id_shape(story["id"])
        assert story["priority"] == 0, story


def f2p_029() -> None:
    _, prd_tools, state_tools, _, _ = _import_impl()
    with workspace():
        prd_tools.omg_create_prd({"title": "D", "stories": [{"title": "S"}]})
        text = _text(prd_tools.omg_read_prd({}))
        summary = json.loads(text)
        assert text == json.dumps(summary, indent=2, ensure_ascii=False), text
        assert "\n" in text, text
        assert "\n" not in _text(state_tools.omg_list_active({}))


def f2p_030() -> None:
    _, _, _, utils, _ = _import_impl()
    ids = [utils.generate_id() for _ in range(20)]
    assert all(len(value) == 8 for value in ids), ids
    assert all(re.fullmatch(r"[0-9a-f]{8}", value) for value in ids), ids
    assert len(set(ids)) == 20, ids
    _assert_updated_at()


def f2p_031() -> None:
    f2p_025()


def f2p_032() -> None:
    f2p_029()


def f2p_033() -> None:
    _assert_phase_info()


def f2p_034() -> None:
    _assert_compact_json()
    _assert_non_ascii_literal()


def f2p_035() -> None:
    _assert_team_line()


def f2p_036() -> None:
    _assert_team_line()


def f2p_037() -> None:
    _assert_all_tools_and_no_delegation()


def f2p_038() -> None:
    _assert_updated_at()


def f2p_039() -> None:
    _assert_compact_json()


def f2p_040() -> None:
    _assert_team_line()


def f2p_041() -> None:
    _assert_all_tools_and_no_delegation()


def f2p_042() -> None:
    _assert_updated_at()


def f2p_043() -> None:
    _assert_compact_json()


def f2p_044() -> None:
    _assert_packaging_entrypoint()


def f2p_045() -> None:
    _assert_all_tools_and_no_delegation()


def f2p_046() -> None:
    _assert_phase_info()
    _, _, _, _, workflow_tools = _import_impl()
    with workspace() as directory:
        state_dir = os.path.join(directory, ".omc", "state")
        os.makedirs(state_dir, exist_ok=True)
        with open(
            os.path.join(state_dir, "omg-autopilot-state.json"), "w", encoding="utf-8"
        ) as handle:
            handle.write(
                _load_fixture(
                    "omg-autopilot-state.json", '{"active":true,"current_phase":4}'
                )
            )
        payload = json.loads(
            _text(workflow_tools.omg_next_phase({"current_phase": 4}))
        )
        assert payload == {
            "success": True,
            "phase": 5,
            "name": "cleanup",
            "description": "State cleanup and completion",
        }, payload


def f2p_047() -> None:
    _assert_phase_info()


def f2p_048() -> None:
    _assert_packaging_entrypoint()


CHECKS = {
    "F2P-025": f2p_025,
    "F2P-026": f2p_026,
    "F2P-027": f2p_027,
    "F2P-028": f2p_028,
    "F2P-029": f2p_029,
    "F2P-030": f2p_030,
    "F2P-031": f2p_031,
    "F2P-032": f2p_032,
    "F2P-033": f2p_033,
    "F2P-034": f2p_034,
    "F2P-035": f2p_035,
    "F2P-036": f2p_036,
    "F2P-037": f2p_037,
    "F2P-038": f2p_038,
    "F2P-039": f2p_039,
    "F2P-040": f2p_040,
    "F2P-041": f2p_041,
    "F2P-042": f2p_042,
    "F2P-043": f2p_043,
    "F2P-044": f2p_044,
    "F2P-045": f2p_045,
    "F2P-046": f2p_046,
    "F2P-047": f2p_047,
    "F2P-048": f2p_048,
}


def run_all() -> dict:
    """Execute every predicate and return ``{observation_id: status}``.

    ``status`` is ``"PASS"`` when the predicate returns cleanly, otherwise
    ``"FAIL: <reason>"``. Exceptions are captured verbatim; nothing is
    swallowed as a false pass.
    """

    results: dict = {}
    for observation_id in sorted(CHECKS):
        try:
            CHECKS[observation_id]()
        except Exception as error:  # noqa: BLE001 - report the raw observation
            results[observation_id] = f"FAIL: {type(error).__name__}: {error}"
        else:
            results[observation_id] = "PASS"
    return results


def main() -> int:
    results = run_all()
    stream = io.StringIO()
    json.dump(results, stream, indent=2, sort_keys=True)
    print(stream.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

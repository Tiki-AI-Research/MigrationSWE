#!/usr/bin/env python3
"""Target-side differential probe for BATCH-DIFFERENTIAL-0001.

Executes the shared case corpus (``shared/batches/BATCH-DIFFERENTIAL-0001/corpus.json``)
against the migrated Python target and emits normalized observations keyed by
observation id. The source probe
(``source/batches/BATCH-DIFFERENTIAL-0001/probe.mjs``) emits the same shape from
the TypeScript source; the grader asserts equality after normalization.

The migration contract fixes only the *public* surface: an importable
``omg_mcp`` package that runs as ``python -m omg_mcp`` and serves the declared
MCP tools over stdio.  Tool, ``tools/list`` and identity observations are
therefore collected over the stdio protocol instead of importing reference
private symbols.  Helper observations (mode validation, JSON safety, id
generation, file safety) call the package helpers whose behaviour the public
contract declares.

This probe never copies or rewrites target files: the target package is
imported read-only from ``--server-dir``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared", "integration"
    ),
)
from mcp_stdio import McpStdioClient  # noqa: E402

MASK_KEYS = ("updated_at", "created_at", "phase_started_at", "completed_at")
MASK_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
MASK_ID_RE = re.compile(r"^story-[0-9a-f]{8}$")


def normalize(value, workspace, norm):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in MASK_KEYS:
                out[key] = "<TS>"
            else:
                out[key] = normalize(item, workspace, norm)
        if isinstance(out.get("active_modes"), list):
            out["active_modes"] = sorted(
                out["active_modes"], key=lambda e: e.get("mode", "") if isinstance(e, dict) else ""
            )
        return out
    if isinstance(value, list):
        out = [normalize(item, workspace, norm) for item in value]
        return out
    if isinstance(value, str):
        text = value
        if workspace:
            text = text.replace(str(workspace), "<WS>")
        if MASK_TS_RE.match(text):
            return "<TS>"
        if MASK_ID_RE.match(text):
            return "story-<ID>"
        return text
    return value


def parse_text(text, workspace, norm):
    try:
        parsed = json.loads(text)
    except Exception:
        return {"json": None, "text": normalize(text, workspace, norm)}
    return {"json": normalize(parsed, workspace, norm), "text": None}


class TargetProbe:
    def __init__(self, server_dir, repo_root, fixtures_dir, corpus):
        self.server_dir = Path(server_dir).resolve()
        self.repo_root = Path(repo_root).resolve()
        self.fixtures_dir = Path(fixtures_dir).resolve()
        self.corpus = corpus
        self.norm = corpus.get("normalization", {})
        sys.path.insert(0, str(self.server_dir))
        self._load_target()

    def _load_target(self):
        # Declared-behaviour helpers (not the tool registry): a migration that
        # exposes the same behaviour elsewhere still drives every tool
        # observation through the MCP protocol.
        self.utils = self._optional_module("omg_mcp.utils")
        self.state_tools = self._optional_module("omg_mcp.state_tools")

    @staticmethod
    def _optional_module(name):
        try:
            return importlib.import_module(name)
        except Exception:  # noqa: BLE001 - helper module is best effort
            return None

    def _require_utils(self):
        if self.utils is None:
            raise ValueError("omg_mcp.utils helper module is absent")

    def fresh_workspace(self):
        return Path(tempfile.mkdtemp(prefix="omg-diff-"))

    def write_dest(self, workspace, dest, content):
        target = workspace / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            target.write_text(content, encoding="utf-8")
        else:
            target.write_text(json.dumps(content, indent=2), encoding="utf-8")

    def apply_setup(self, workspace, entries):
        for entry in entries or []:
            dest = entry["dest"]
            if "fixture" in entry:
                source = self.fixtures_dir / entry["fixture"]
                target = workspace / dest
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            elif "raw" in entry:
                self.write_dest(workspace, dest, entry["raw"])
            else:
                self.write_dest(workspace, dest, entry.get("content"))

    def preset_full_store(self, workspace):
        entries = [
            {"key": f"k{i}", "value": "v", "category": "project",
             "created_at": "2020-01-01T00:00:00.000Z", "updated_at": "2020-01-01T00:00:00.000Z"}
            for i in range(500)
        ]
        self.write_dest(workspace, ".omc/project-memory.json", {"entries": entries})

    def resolve_placeholder(self, workspace, token):
        if token == "<missing>":
            return str(workspace / ".omc" / "does-not-exist.json")
        if token == "<symlink>":
            real = workspace / ".omc" / "real.txt"
            link = workspace / ".omc" / "link.txt"
            real.parent.mkdir(parents=True, exist_ok=True)
            real.write_text("data", encoding="utf-8")
            if link.exists() or link.is_symlink():
                link.unlink()
            os.symlink(real, link)
            return str(link)
        if token == "<oversize>":
            big = workspace / ".omc" / "big.txt"
            big.parent.mkdir(parents=True, exist_ok=True)
            with open(big, "w", encoding="utf-8") as handle:
                handle.write("x" * (5 * 1024 * 1024 + 16))
            return str(big)
        if token == "<regular>":
            regular = workspace / ".omc" / "regular.txt"
            regular.parent.mkdir(parents=True, exist_ok=True)
            regular.write_text("hello", encoding="utf-8")
            return str(regular)
        if token == "<newfile>":
            return str(workspace / ".omc" / "created.txt")
        return token

    @staticmethod
    def _block_text(block):
        if isinstance(block, dict):
            return block.get("type"), block.get("text")
        return getattr(block, "type", None), getattr(block, "text", None)

    def capture_blocks(self, result, workspace):
        if isinstance(result, dict):
            content = result.get("content") or []
        else:
            content = getattr(result, "content", []) or []
        blocks = []
        for block in content:
            block_type, text = self._block_text(block)
            blocks.append(
                {
                    "type": block_type,
                    "result": parse_text(text, workspace, self.norm) if text is not None else None,
                }
            )
        return {"content_length": len(blocks), "blocks": blocks}

    def _public_call(self, workspace, name, arguments):
        client = McpStdioClient(str(self.server_dir), env_extra={"WORKSPACE_ROOT": str(workspace)})
        try:
            client.start()
            result = client.call_tool(name, arguments)
            blocks = result.get("content", [])
            if len(blocks) != 1 or blocks[0].get("type") != "text":
                raise ValueError("tool did not return the required text envelope")
            return blocks[0]["text"]
        finally:
            client.close()

    def run_module(self, workspace, name, args):
        # Helpers have no mandated Python names or return types. Exercise their
        # required behavior through the public tools and inspect real files.
        if name == "validate_mode":
            result = json.loads(self._public_call(workspace, "omg_read_state", {"mode": args[0]}))
            if result.get("success") is False:
                return {"ok": False, "error": result.get("error")}
            return {"ok": True}
        if name == "safe_json_parse":
            path = workspace / ".omc/state/json-probe-state.json"
            try:
                payload = json.loads(self._public_call(workspace, "omg_write_state", {"mode": "json-probe", "state": args[0]}))
                if payload.get("success") is False:
                    return {"ok": False, "data": None, "error": payload.get("error")}
                if payload.get("success") is not True or not isinstance(payload.get("state"), dict):
                    raise ValueError("omg_write_state did not return its required envelope")
                data = json.loads(path.read_text(encoding="utf-8"))
                data.pop("updated_at", None)
                return {"ok": True, "data": data, "error": None}
            finally:
                if path.exists():
                    path.unlink()
        if name == "generate_id":
            self._public_call(workspace, "omg_create_prd", {"title": "IDs", "stories": [{"title": str(i)} for i in range(20)]})
            stories = json.loads((workspace / ".omc/prd.json").read_text())["stories"]
            values = []
            for story in stories:
                identifier = story["id"]
                if not isinstance(identifier, str) or not identifier.startswith("story-"):
                    raise ValueError("generated story identifier has no required prefix")
                values.append(identifier.removeprefix("story-"))
            return {"lengths": sorted({len(v) for v in values}), "unique": len(set(values))}
        if name in {"safe_read_file", "safe_write_file"}:
            source = Path(self.resolve_placeholder(workspace, args[0]))
            mode = "file-probe"
            path = workspace / f".omc/state/{mode}-state.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() or path.is_symlink():
                path.unlink()
            if source.is_symlink():
                path.symlink_to(os.readlink(source))
            elif source.is_file():
                shutil.copyfile(source, path)
            if name == "safe_read_file":
                text = self._public_call(workspace, "omg_read_state", {"mode": mode})
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = None
                if payload == {"active": False, "mode": mode, "message": "No state found"}:
                    return {"value": None}
                if args[0] == "<newfile>" and isinstance(payload, dict):
                    return {"value": payload.get("value")}
                return {"value": text}
            payload = json.loads(self._public_call(workspace, "omg_write_state", {"mode": mode, "state": json.dumps({"value": args[1]})}))
            if payload.get("success") is False:
                return {"ok": False, "error": payload.get("error")}
            if payload.get("success") is not True or path.is_symlink():
                raise ValueError("safe write did not create a regular state file")
            persisted = json.loads(path.read_text())
            if persisted.get("value") != args[1]:
                raise ValueError("safe write did not persist the supplied value")
            shutil.copyfile(path, source)
            return {"ok": True}
        if name == "get_state_path":
            self._public_call(workspace, "omg_write_state", {"mode": args[0], "state": "{}"})
            path = workspace / f".omc/state/{args[0]}-state.json"
            if not path.is_file():
                raise ValueError("state was not persisted at the required path")
            return {"path": str(path)}
        raise ValueError(f"unknown module call: {name}")

    @staticmethod
    def _attr(value, name):
        if isinstance(value, dict):
            return value.get(name)
        return getattr(value, name, None)

    def run_step(self, workspace, step, get_client):
        if "setup" in step:
            self.apply_setup(workspace, step["setup"])
        if step.get("preset_full_store"):
            self.preset_full_store(workspace)
        if "tool" in step:
            args = dict(step.get("args", {}))
            if args.get("value") == "<oversize>":
                args["value"] = "x" * 10001
            result = get_client().call_tool(step["tool"], args)
            return {"step": "tool", "tool": step["tool"], "captured": self.capture_blocks(result, workspace)}
        if "read" in step:
            path = workspace / step["read"]
            raw = path.read_text(encoding="utf-8")
            try:
                parsed = json.loads(raw)
                return {"step": "read", "path": step["read"], "captured": normalize(parsed, workspace, self.norm)}
            except Exception:
                return {"step": "read", "path": step["read"], "captured": normalize(raw, workspace, self.norm)}
        if "read_dir" in step:
            directory = workspace / step["read_dir"]
            names = sorted(p.name for p in directory.iterdir()) if directory.exists() else []
            return {"step": "read_dir", "path": step["read_dir"], "captured": names}
        if "module" in step:
            value = self.run_module(workspace, step["module"], step.get("args", []))
            return {"step": "module", "module": step["module"], "captured": normalize(value, workspace, self.norm)}
        if step.get("tools"):
            tools = get_client().list_tools()
            captured = [
                {"name": tool.get("name"), "inputSchema": tool.get("inputSchema", {})}
                for tool in tools
            ]
            return {"step": "tools", "captured": captured}
        if step.get("identity"):
            info = get_client().server_info()
            return {
                "step": "identity",
                "captured": {"name": info.get("name"), "version": info.get("version")},
            }
        if "hash_paths" in step:
            captured = {}
            for rel in step["hash_paths"]:
                path = self.repo_root / rel
                if path.is_dir():
                    captured[rel] = sorted(
                        str(p.relative_to(path)) for p in path.rglob("*") if p.is_file()
                    )
                elif path.is_file():
                    captured[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
                else:
                    captured[rel] = None
            return {"step": "hash_paths", "captured": captured}
        raise ValueError(f"unhandled step: {step}")

    def run_case(self, case):
        if case.get("static"):
            return {"id": case["id"], "static": True, "note": case.get("note")}
        workspace = self.fresh_workspace()
        previous_root = os.environ.get("WORKSPACE_ROOT")
        os.environ["WORKSPACE_ROOT"] = str(workspace)
        holder = {"client": None}

        def get_client():
            if holder["client"] is None:
                client = McpStdioClient(
                    str(self.server_dir), env_extra={"WORKSPACE_ROOT": str(workspace)}
                )
                client.start()
                holder["client"] = client
            return holder["client"]

        try:
            observations = []
            for step in case.get("steps", []):
                observations.append(self.run_step(workspace, step, get_client))
            return {"id": case["id"], "steps": observations}
        finally:
            if holder["client"] is not None:
                holder["client"].close()
            if previous_root is None:
                os.environ.pop("WORKSPACE_ROOT", None)
            else:
                os.environ["WORKSPACE_ROOT"] = previous_root
            shutil.rmtree(workspace, ignore_errors=True)

    def run(self):
        output = []
        for case in self.corpus["cases"]:
            output.append(self.run_case(case))
        return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--server-dir", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()

    with open(args.corpus, "r", encoding="utf-8") as handle:
        corpus = json.load(handle)

    probe = TargetProbe(args.server_dir, args.repo_root, args.fixtures, corpus)
    observations = probe.run()
    payload = {"batch_id": corpus.get("batch_id"), "probe": "target", "observations": observations}
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()

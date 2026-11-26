"""Minimal MCP stdio client used by the target-side probes.

The migration contract only fixes the *public* MCP surface: an importable
``omg_mcp`` package that runs as ``python -m omg_mcp`` and speaks the stdio
JSON-RPC protocol (``initialize`` / ``tools/list`` / ``tools/call``).  The
reference implementation happens to expose additional private helpers, but a
legal migration may organise its internal modules and function signatures
freely.  Probes therefore drive the candidate through the protocol instead of
importing reference-private symbols.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time


class McpStdioError(RuntimeError):
    """Raised when the stdio server cannot answer a request in time."""


class McpStdioClient:
    def __init__(self, mcp_root, *, env_extra=None, timeout=40.0, cwd=None):
        self.mcp_root = os.path.abspath(mcp_root)
        self.timeout = timeout
        environment = dict(os.environ)
        environment["PYTHONPATH"] = (
            self.mcp_root + os.pathsep + environment.get("PYTHONPATH", "")
        )
        environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        if env_extra:
            environment.update({k: str(v) for k, v in env_extra.items() if v is not None})
        self._environment = environment
        self._cwd = cwd or self.mcp_root
        self.proc = None
        self._id = 0
        self._initialized = False
        self._server_info = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self.proc is not None:
            return self
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "omg_mcp"],
            cwd=self._cwd,
            env=self._environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return self

    def initialize(self):
        if self._initialized:
            return {"result": {"serverInfo": self._server_info}}
        reply = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "1.0.0"},
            },
        )
        self._initialized = True
        self._server_info = reply.get("result", {}).get("serverInfo", {})
        self.notify("notifications/initialized")
        return reply

    def close(self):
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # -- protocol ----------------------------------------------------------
    def _write(self, message):
        payload = json.dumps(message) + "\n"
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def request(self, method, params=None, timeout=None):
        self.start()
        if method != "initialize" and not self._initialized:
            self.initialize()
        self._id += 1
        request_id = self._id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)
        return self._read_reply(request_id, timeout=timeout)

    def _read_reply(self, request_id, timeout=None):
        deadline = time.time() + (self.timeout if timeout is None else timeout)
        stream = self.proc.stdout
        while time.time() < deadline:
            ready, _, _ = select.select([stream], [], [], max(0.0, deadline - time.time()))
            if not ready:
                break
            line = stream.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id:
                return message
        raise McpStdioError(f"no reply for {request_id} within timeout")

    # -- convenience -------------------------------------------------------
    def server_info(self):
        reply = self.initialize()
        return reply.get("result", {}).get("serverInfo", {})

    def list_tools(self):
        reply = self.request("tools/list", {})
        return reply.get("result", {}).get("tools", [])

    def call_tool(self, name, arguments):
        reply = self.request("tools/call", {"name": name, "arguments": arguments})
        return reply.get("result", {})

    def call_tool_text(self, name, arguments):
        result = self.call_tool(name, arguments)
        blocks = result.get("content") or []
        if not blocks:
            raise McpStdioError(f"tool {name} returned no content")
        return blocks[0].get("text")


def shared_integration_dir(probe_file):
    """Return the ``shared/integration`` directory for a batch probe file."""
    here = os.path.dirname(os.path.abspath(probe_file))
    # probes live at <assets>/{target,shared}/batches/<batch>/<file>, so the
    # shared integration helper is three levels up (or two from a shared batch).
    for relative in ("../../../shared/integration", "../../shared/integration"):
        candidate = os.path.abspath(os.path.join(here, relative))
        if os.path.isfile(os.path.join(candidate, "mcp_stdio.py")):
            return candidate
    raise McpStdioError("cannot locate shared integration assets")


def import_mcp_stdio(probe_file):
    directory = shared_integration_dir(probe_file)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    import mcp_stdio  # noqa: E402

    return mcp_stdio

"""Shared differential harness for the TypeScript 6.0.3 -> Go 1.23.0 migration.

The harness drives a release-action implementation through the same declarative
case corpus and records observable behaviour only:

  * process exit code
  * the ordered sequence of GitHub REST requests (method, path, JSON body or
    upload metadata)
  * the action outputs written to GITHUB_OUTPUT
  * the normalized ``::error::`` diagnostic, when the action fails

Human readable log text is never part of the contract: request bodies are
compared as parsed JSON and diagnostics are whitespace-normalized. Every
subprocess is bounded by an explicit timeout so the harness can never block.
"""

from __future__ import annotations

import copy
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_TIMEOUT = 60


def _go_list_packages(text: str) -> list[dict[str, Any]]:
    """Decode a stream of concatenated ``go list -json`` objects."""
    decoder = json.JSONDecoder()
    packages: list[dict[str, Any]] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        if index >= len(text):
            break
        value, end = decoder.raw_decode(text, index)
        packages.append(value)
        index = end
    return packages


def select_main_package(
    packages: list[dict[str, Any]],
    *,
    action_text: str = "",
    docker_text: str = "",
    repo_root: str = "",
) -> str | None:
    """Pick the candidate action executable from ``go list -json`` output.

    The migration may place Go under ``src/`` and ``cmd/`` (or any other
    layout), so the probe must not assume a flat top-level ``package main``.
    A single main package is used directly; multiple candidates are
    disambiguated by the ``action.yml``/``Dockerfile`` reference to the package
    directory, matching the public behavior adapter.
    """
    mains = [
        package for package in packages
        if isinstance(package, dict)
        and str(package.get("Name")) == "main"
        and str(package.get("ImportPath") or "").strip()
    ]
    if len(mains) == 1:
        return str(mains[0]["ImportPath"])
    if not mains:
        return None
    haystack = (action_text or "") + "\n" + (docker_text or "")
    selected: list[dict[str, Any]] = []
    root = str(repo_root).rstrip("/")
    for package in mains:
        directory = str(package.get("Dir") or "")
        if root and directory.startswith(root):
            relative = "./" + directory[len(root):].lstrip("/")
            if relative in haystack:
                selected.append(package)
    if len(selected) == 1:
        return str(selected[0]["ImportPath"])
    return None


def discover_main_package(repo_root: str, env: dict[str, str] | None = None, timeout: int = 120) -> str | None:
    """Resolve the candidate action's main package import path."""

    environment = dict(os.environ)
    if env:
        environment.update(env)
    completed = subprocess.run(
        ["go", "list", "-json", "./..."],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError("go list failed:\n" + (completed.stderr or ""))
    action_text = ""
    action_path = os.path.join(repo_root, "action.yml")
    if os.path.isfile(action_path):
        with open(action_path, "r", encoding="utf-8") as handle:
            action_text = handle.read()
    docker_text = ""
    docker_path = os.path.join(repo_root, "Dockerfile")
    if os.path.isfile(docker_path):
        with open(docker_path, "r", encoding="utf-8") as handle:
            docker_text = handle.read()
    return select_main_package(
        _go_list_packages(completed.stdout),
        action_text=action_text,
        docker_text=docker_text,
        repo_root=str(repo_root),
    )

# action.yml defaults, as a GitHub runner materializes them for the action.
# ``token`` is intentionally absent: the corpus controls it so the required
# token contract can be probed with blank/absent values.
ACTION_INPUT_DEFAULTS = {
    "INPUT_allowUpdates": "",
    "INPUT_artifactErrorsFailBuild": "",
    "INPUT_artifact": "",
    "INPUT_artifacts": "",
    "INPUT_artifactContentType": "",
    "INPUT_body": "",
    "INPUT_bodyFile": "",
    "INPUT_commit": "",
    "INPUT_discussionCategory": "",
    "INPUT_draft": "",
    "INPUT_generateReleaseNotes": "false",
    "INPUT_generateReleaseNotesPreviousTag": "",
    "INPUT_immutableCreate": "false",
    "INPUT_makeLatest": "legacy",
    "INPUT_name": "",
    "INPUT_omitBody": "false",
    "INPUT_omitBodyDuringUpdate": "false",
    "INPUT_omitDraftDuringUpdate": "false",
    "INPUT_omitName": "false",
    "INPUT_omitNameDuringUpdate": "false",
    "INPUT_omitPrereleaseDuringUpdate": "false",
    "INPUT_owner": "",
    "INPUT_prerelease": "",
    "INPUT_removeArtifacts": "false",
    "INPUT_replacesArtifacts": "true",
    "INPUT_repo": "",
    "INPUT_skipIfReleaseExists": "false",
    "INPUT_tag": "",
    "INPUT_updateOnlyUnreleased": "false",
}

# Volatile headers are not part of the observable contract.
_IGNORED_HEADERS = {
    "host",
    "content-length",
    "connection",
    "accept-encoding",
}


def _env_key(name: str) -> str:
    """GitHub runners expose inputs as uppercase INPUT_* env variables."""
    if name.startswith("INPUT_"):
        return "INPUT_" + name[len("INPUT_") :].upper()
    return name


def normalize_whitespace(value: str) -> str:
    """Collapse runs of whitespace so formatting is never the contract."""
    return re.sub(r"\s+", " ", value).strip()


class _Recorder:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        # Responses are consumed in order per (method, path) so a single
        # endpoint can produce a deterministic sequence.
        self._responses = [dict(r) for r in responses]
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.base_url = ""

    def next_response(self, method: str, path: str) -> dict[str, Any]:
        with self._lock:
            for index, response in enumerate(self._responses):
                if response.get("method", method) != method:
                    continue
                if not re.search(response.get("path", ".*"), path):
                    continue
                self._responses.pop(index)
                return response
        return {"status": 404, "body": {"message": "Not Found"}}


def _substitute(value: Any, base_url: str) -> Any:
    if isinstance(value, str):
        return value.replace("{base}", base_url)
    if isinstance(value, list):
        return [_substitute(item, base_url) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, base_url) for key, item in value.items()}
    return value


def _make_handler(recorder: _Recorder):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # silence server logs
            return

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            content_type = self.headers.get("content-type", "")
            path = self.path

            body_json: Any = None
            if raw and "json" in content_type:
                try:
                    body_json = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body_json = None

            record: dict[str, Any] = {
                "method": method,
                "path": path.split("?", 1)[0],
                "query": path.split("?", 1)[1] if "?" in path else "",
                "content_type": content_type.split(";", 1)[0].strip(),
                "content_length": length,
            }
            if body_json is not None:
                record["body"] = body_json
            if "json" not in content_type and raw:
                record["upload"] = True
            recorder.requests.append(record)

            response = recorder.next_response(method, record["path"])
            status = int(response.get("status", 200))
            payload = response.get("body", {})
            payload = _substitute(payload, recorder.base_url)
            if isinstance(payload, (dict, list)):
                encoded = json.dumps(payload).encode("utf-8")
                ctype = "application/json"
            else:
                encoded = str(payload).encode("utf-8")
                ctype = response.get("content_type", "text/plain")

            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PATCH(self) -> None:
            self._handle("PATCH")

        def do_PUT(self) -> None:
            self._handle("PUT")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

    return Handler


class MockGithub:
    """A minimal, programmable GitHub REST server that records requests."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.recorder = _Recorder(responses)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.recorder))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "MockGithub":
        self.recorder.base_url = self.base_url
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def parse_outputs(path: str) -> dict[str, str]:
    """Parse a GITHUB_OUTPUT file (delimiter or key=value form)."""
    if not os.path.exists(path):
        return {}
    outputs: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        if "<<" in line:
            name, delimiter = line.split("<<", 1)
            values: list[str] = []
            index += 1
            while index < len(lines) and lines[index] != delimiter:
                values.append(lines[index])
                index += 1
            outputs[name] = "\n".join(values)
        elif "=" in line:
            name, value = line.split("=", 1)
            outputs[name] = value
        index += 1
    return outputs


def extract_error(stderr: str) -> str:
    """Return the whitespace-normalized ``::error::`` diagnostic, if any.

    Multi-line diagnostics are preserved (only whitespace is normalized) so the
    structured error payload, not its formatting, is compared.
    """
    lines = stderr.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("::error::"):
            continue
        payload = [line[len("::error::") :]]
        for follow in lines[index + 1 :]:
            if follow.startswith("::"):
                break
            payload.append(follow)
        return normalize_whitespace("\n".join(payload))
    return ""


def normalize_result(exit_code: int, outputs: dict[str, str], stderr: str, requests: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "exit_code": exit_code,
        "requests": requests,
        "outputs": outputs,
        "error": extract_error(stderr),
    }


def run_case(
    case: dict[str, Any],
    command: list[str],
    repo_root: str,
    timeout: int = DEFAULT_TIMEOUT,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run ``command`` for one case and return its normalized observation.

    The subprocess is always bounded by ``timeout``; a timeout is reported as a
    distinct observation rather than hanging the harness.
    """
    workdir = tempfile.mkdtemp(prefix="diff-case-")
    home = os.path.join(workdir, "home")
    os.makedirs(home, exist_ok=True)
    output_path = os.path.join(workdir, "github_output")
    # GitHub creates GITHUB_OUTPUT before the step runs; a candidate that opens
    # it without O_CREATE is not defective.
    open(output_path, "w").close()

    try:
        for entry in case.get("workspace", []):
            target = os.path.join(workdir, entry["path"])
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(entry.get("content", ""))
        for relative, content in case.get("home", {}).items():
            target = os.path.join(home, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(content)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": home,
            "GITHUB_OUTPUT": output_path,
            "GITHUB_WORKSPACE": workdir,
            "GITHUB_REPOSITORY": "ncipollo/release-action",
            "GITHUB_REF": "refs/tags/v1.2.3",
        }
        env.update({_env_key(name): str(value) for name, value in ACTION_INPUT_DEFAULTS.items()})
        env.update({_env_key(name): str(value) for name, value in case.get("env", {}).items()})
        if extra_env:
            env.update(extra_env)

        with MockGithub(case.get("github", [])) as mock:
            env["GITHUB_API_URL"] = mock.base_url
            try:
                completed = subprocess.run(
                    command,
                    cwd=workdir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                exit_code = completed.returncode
                # GitHub workflow commands may be written to either stream, so
                # diagnostics are collected from both.
                stderr = (completed.stdout or "") + "\n" + (completed.stderr or "")
            except subprocess.TimeoutExpired as exc:
                exit_code = 124
                stderr = f"timed out after {timeout}s: {exc}"
            requests = copy.deepcopy(mock.recorder.requests)
            base_url = mock.base_url

        outputs = {
            name: value.replace(base_url, "{base}")
            for name, value in parse_outputs(output_path).items()
        }
        return normalize_result(exit_code, outputs, stderr, requests)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _strip_runs_block(text: str) -> str:
    """Return action.yml text with the ``runs:`` block removed.

    The ``runs`` section is the only part the migration may adapt, so the
    invariant projection of ``action.yml`` excludes it.
    """
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if re.match(r"^runs:\s*$", line):
            index += 1
            while index < len(lines) and (not lines[index].strip() or lines[index].startswith(" ")):
                index += 1
            continue
        out.append(line.rstrip())
        index += 1
    return "\n".join(part for part in out if part.strip())


def _sha256(text: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def action_schema_invariant(repo_root: str) -> dict[str, Any]:
    """Project action.yml down to the parts that must not change."""
    path = os.path.join(repo_root, "action.yml")
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    inputs: list[str] = []
    outputs: list[str] = []
    branding: dict[str, str] = {}
    section = ""
    for line in text.split("\n"):
        if not line.strip():
            continue
        if not line.startswith(" "):
            section = line.split(":", 1)[0].strip()
            continue
        if len(line) - len(line.lstrip(" ")) != 2:
            continue
        key = line.strip().split(":", 1)[0]
        if section == "inputs":
            inputs.append(key)
        elif section == "outputs":
            outputs.append(key)
        elif section == "branding":
            branding[key] = line.strip().split(":", 1)[1].strip()

    return {
        "inputs": inputs,
        "outputs": outputs,
        "branding": branding,
        "invariant_sha256": _sha256(_strip_runs_block(text)),
    }


def test_suite_integrity(repo_root: str) -> dict[str, Any]:
    """Report whether the migrated test suite weakens or skips assertions."""
    go_tests = glob.glob(os.path.join(repo_root, "**", "*_test.go"), recursive=True)
    if go_tests:
        weakened = False
        for path in go_tests:
            with open(path, "r", encoding="utf-8") as handle:
                if "t.Skip(" in handle.read():
                    weakened = True
        return {"suite": "go", "test_files": len(go_tests), "weakens_assertions": weakened}

    ts_tests = glob.glob(os.path.join(repo_root, "__tests__", "**", "*.ts"), recursive=True)
    weakened = False
    for path in ts_tests:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
            if ".only(" in content or "xit(" in content:
                weakened = True
    return {"suite": "typescript", "test_files": len(ts_tests), "weakens_assertions": weakened}


def run_static_case(case: dict[str, Any], repo_root: str) -> dict[str, Any]:
    check = case.get("static_check")
    if check == "action_schema_invariant":
        value = action_schema_invariant(repo_root)
    elif check == "test_suite_integrity":
        value = test_suite_integrity(repo_root)
    else:
        value = {"error": f"unknown static check: {check}"}
    return {"kind": "static", "check": check, "value": value}


def evaluate_case(
    case: dict[str, Any],
    command: list[str],
    repo_root: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    if case.get("kind") == "static":
        return run_static_case(case, repo_root)
    return run_case(case, command, repo_root, timeout)


def load_cases(path: str) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)["cases"]


def results_by_observation(cases: list[dict[str, Any]], results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Group case results under every observation they cover."""
    grouped: dict[str, Any] = {}
    for case in cases:
        result = results[case["id"]]
        for observation_id in case["observation_ids"]:
            grouped.setdefault(observation_id, {})[case["id"]] = result
    return grouped

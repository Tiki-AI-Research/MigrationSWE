#!/usr/bin/env python3
"""Contract-behaviour probes for the migrated Go labeler action.

The migration contract fixes the action's *observable* behaviour (the GitHub
Actions runtime interface: ``INPUT_*`` environment variables, ``GITHUB_EVENT_PATH``,
``GITHUB_OUTPUT`` and the GitHub API/GraphQL endpoints named by ``GITHUB_API_URL``
and ``GITHUB_GRAPHQL_URL``).  It does not fix the internal Go function signatures
of the migrated packages, and the reference implementation is not the only legal
package layout.

These probes therefore build the candidate's ``cmd/labeler`` entrypoint and drive
it exactly the way GitHub Actions does, against an in-process fake GitHub API.
They assert the documented behaviour through that public interface only, so a
legal implementation with different internal signatures is not rejected on an
unrelated compilation error.
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.environ.get("MIGRATION_REPO_ROOT") or os.environ.get("REPO_ROOT") or os.getcwd()

# ---------------------------------------------------------------------------
# Configuration fixtures.
# ---------------------------------------------------------------------------

BASIC = "documentation:\n  - changed-files:\n      - any-glob-to-any-file: ['docs/**']\n"
ORDERING = (
    "b:\n  - changed-files:\n      - any-glob-to-any-file: ['*.txt']\n"
    "a:\n  - changed-files:\n      - any-glob-to-any-file: ['*.txt']\n"
    "c:\n  - changed-files:\n      - any-glob-to-any-file: ['*.txt']\n"
)
STALE = "stale:\n  - head-branch: '^never$'\n"
STALE2 = "stale:\n  - head-branch: '^never$'\nstale2:\n  - head-branch: '^never$'\n"
NEG_LIMIT = "changed-files-labels-limit: -1\nthing:\n  - changed-files:\n      - any-glob-to-any-file: ['**/*.go']\n"
UNKNOWN_KEY = (
    "mylabel:\n  - changed-files:\n      - any-glob-to-any-file: ['**/*.go']\n"
    "    unknown-option: bar\n"
)
MAX_FILES = (
    "changed-files-labels-limit: 2\nmax-files-changed: 1\n"
    "thing:\n  - changed-files:\n      - any-glob-to-any-file: ['**/*.go']\n"
)
DOT = "touched:\n  - changed-files:\n      - any-glob-to-any-file: ['*.pdf']\n"
NEGATION = "notdocs:\n  - changed-files:\n      - any-glob-to-any-file: ['!docs/**']\n"
BRANCH_RELEASE = "rel:\n  - head-branch: '^release/\\d+\\.\\d+\\.\\d+$'\n"
BRANCH_ECMA = "not-release:\n  - head-branch: '^(?!main$).*'\n"
BRANCH_ONE_CODE_UNIT = "one-unit:\n  - head-branch: '^.$'\n"
BRANCH_TWO_CODE_UNITS = "two-units:\n  - head-branch: '^..$'\n"


def cap_config(count):
    lines = []
    for i in range(count):
        lines.append("label-%03d:\n  - changed-files:\n      - any-glob-to-any-file: ['**/*.go']\n" % i)
    return "".join(lines)


# ---------------------------------------------------------------------------
# Fake GitHub API.
# ---------------------------------------------------------------------------

class State:
    def __init__(self):
        self.pr_number = 123
        self.pr_node_id = "PR_node_1"
        self.pr_labels = []          # list of {"name":..., "node_id":...}
        self.changed = []            # page 1 filenames
        self.file_page2 = None       # page 2 filenames (when paginating)
        self.link_next = False
        self.content = None          # config yaml returned by contents API
        self.content_status = 200
        self.add_status = 200
        self.recovery_pages = None   # list of list[str] for GET issue labels
        self.pull_status = 200
        self.head = "main"
        self.base = "main"
        self.remove_error = False
        self.add_message = "Server Error"
        self.verify_status = 200
        self.removed = []
        self.added = []
        self.graphql = []
        self.rest_deletes = 0
        self.file_pages = []
        self.label_pages = []
        self.requests = []
        self.authorizations = []


class Handler(BaseHTTPRequestHandler):
    state = None

    def log_message(self, *args):
        pass

    def _send(self, status, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path(self):
        return urllib.parse.urlparse(self.path).path

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def do_GET(self):
        s = self.state
        path = self._path()
        s.requests.append(("GET", self.path))
        s.authorizations.append(self.headers.get('Authorization', ''))
        if path.endswith("/graphql"):
            return self._send(200, {"data": {}})
        if "/pulls/" in path and path.endswith("/files"):
            page = (self._query().get("page") or ["1"])[0]
            s.file_pages.append(page)
            if page == "2" and s.file_page2 is not None:
                return self._send(200, [{"filename": f} for f in s.file_page2])
            headers = {}
            if s.link_next:
                headers["Link"] = '<%s%s?per_page=100&page=2>; rel="next"' % (s.api_url, path)
            return self._send(200, [{"filename": f} for f in s.changed], headers)
        if "/pulls/" in path:
            if s.pull_status != 200:
                return self._send(s.pull_status, {"message": "Not Found"})
            return self._send(200, {
                "number": int(path.rstrip("/").rsplit("/", 1)[-1]),
                "node_id": s.pr_node_id,
                "labels": s.pr_labels,
                "head": {"ref": s.head},
                "base": {"ref": s.base},
            })
        if "/contents/" in path:
            if s.content_status != 200:
                return self._send(s.content_status, {"message": "Not Found"})
            encoded = base64.b64encode((s.content or "").encode()).decode()
            return self._send(200, {"content": encoded, "encoding": "base64"})
        if path.endswith("/labels"):
            if s.verify_status != 200:
                return self._send(s.verify_status, {"message": "Verification failed"})
            page = int((self._query().get("page") or ["1"])[0])
            s.label_pages.append(page)
            if s.recovery_pages:
                if page <= len(s.recovery_pages):
                    headers = {}
                    if page < len(s.recovery_pages):
                        headers["Link"] = '<%s%s?per_page=100&page=%d>; rel="next"' % (s.api_url, path, page + 1)
                    return self._send(200, [{"name": n} for n in s.recovery_pages[page - 1]], headers)
                return self._send(200, [])
            return self._send(200, [])
        return self._send(404, {"message": "Not Found"})

    def do_POST(self):
        s = self.state
        path = self._path()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        s.requests.append(("POST", self.path))
        s.authorizations.append(self.headers.get('Authorization', ''))
        if path.endswith("/graphql"):
            try:
                body = json.loads(raw.decode() or "{}")
            except ValueError:
                body = {}
            s.graphql.append(body)
            if s.remove_error:
                return self._send(200, {"errors": [{"message": "Removal failed"}]})
            return self._send(200, {"data": {}})
        if path.endswith("/labels"):
            try:
                body = json.loads(raw.decode() or "{}")
            except ValueError:
                body = {}
            s.added.append(body.get("labels", []))
            if s.add_status >= 400:
                return self._send(s.add_status, {"message": s.add_message})
            return self._send(200, [])
        return self._send(404, {"message": "Not Found"})

    def do_DELETE(self):
        self.state.rest_deletes += 1
        self.state.requests.append(("DELETE", self.path))
        self._send(200, {})

    def do_PATCH(self):
        self._send(404, {"message": "Not Found"})


def start_server(state):
    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------------------
# Action runner.
# ---------------------------------------------------------------------------

def build_binary():
    # A cache shared by different checkouts must never replay another candidate.
    digest = hashlib.sha256(os.path.realpath(REPO).encode())
    for directory, dirs, files in os.walk(REPO):
        dirs[:] = sorted(d for d in dirs if d not in
                         (".git", ".gomodcache", ".gocache", "node_modules", "vendor"))
        for name in sorted(files):
            if name.endswith(".go") and not name.endswith("_test.go") or name in ("go.mod", "go.sum"):
                path = os.path.join(directory, name)
                digest.update(os.path.relpath(path, REPO).encode())
                with open(path, "rb") as handle:
                    digest.update(handle.read())
    binary = os.path.join(tempfile.gettempdir(), "labeler-blackbox-" + digest.hexdigest())
    if os.path.isfile(binary):
        return binary
    proc = subprocess.run(
        ["go", "build", "-o", binary, "./cmd/labeler"],
        cwd=REPO, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("go build ./cmd/labeler failed:\n" + proc.stdout + proc.stderr)
    return binary


def parse_outputs(text):
    """Parse both documented GITHUB_OUTPUT encodings: heredoc and name=value."""
    out = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("::set-output name="):
            rest = line[len("::set-output name="):]
            if "::" in rest:
                name, value = rest.split("::", 1)
                out[name] = value
            i += 1
            continue
        if "<<" in line:
            name, delim = line.split("<<", 1)
            value = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                value.append(lines[i])
                i += 1
            out[name] = "\n".join(value)
            i += 1
            continue
        if line and "=" in line:
            name, _, value = line.partition("=")
            if name:
                out[name] = value
        i += 1
    return out


def run_action(binary, state, config_text, *, local_config=True, dot=True, head="main",
               base="main", sync=False, pr_input="123", changed=None, pr_labels=None,
               config_path_missing=None, relative_config=False, local_config_path=None, token='token'):
    if changed is not None:
        state.changed = changed
    if pr_labels is not None:
        state.pr_labels = pr_labels
    state.head, state.base = head, base
    tmp = tempfile.mkdtemp(prefix="labeler-bb-")
    if local_config:
        config_path = os.path.join(tmp, local_config_path) if local_config_path else (
            os.path.join(tmp, ".github", "labeler.yml") if relative_config else os.path.join(tmp, "labeler.yml"))
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as handle:
            handle.write(config_text)
    elif config_path_missing is not None:
        config_path = config_path_missing
    else:
        config_path = os.path.join(tmp, "missing", "labeler.yml")
        state.content = config_text
    event_path = os.path.join(tmp, "event.json")
    with open(event_path, "w") as handle:
        json.dump({"pull_request": {
            "number": state.pr_number,
            "node_id": state.pr_node_id,
            "head": {"ref": head},
            "base": {"ref": base},
            "labels": state.pr_labels,
        }}, handle)
    out_path = os.path.join(tmp, "output")
    # The Actions runner creates command files before starting the action.
    with open(out_path, "w"):
        pass
    env = dict(os.environ)
    env.update({
        "GITHUB_API_URL": state.api_url,
        "GITHUB_GRAPHQL_URL": state.api_url + "/graphql",
        "GITHUB_REPOSITORY": "monalisa/helloworld",
        "GITHUB_SHA": "sha",
        "GITHUB_EVENT_PATH": event_path,
        "GITHUB_OUTPUT": out_path,
        "INPUT_REPO-TOKEN": token,
        "INPUT_CONFIGURATION-PATH": os.path.relpath(config_path, tmp) if relative_config else config_path,
        "INPUT_SYNC-LABELS": "true" if sync else "false",
        "INPUT_DOT": "true" if dot else "false",
    })
    if pr_input is None:
        env.pop("INPUT_PR-NUMBER", None)
    else:
        env["INPUT_PR-NUMBER"] = pr_input
    if callable(binary):
        code, log, outputs = binary(env, tmp)
        shutil.rmtree(tmp, ignore_errors=True)
        return code, log, outputs
    command = list(binary) if isinstance(binary, (list, tuple)) else [binary]
    proc = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    outputs = {}
    if os.path.exists(out_path):
        with open(out_path) as handle:
            outputs = parse_outputs(handle.read())
    shutil.rmtree(tmp, ignore_errors=True)
    return proc.returncode, proc.stdout + proc.stderr, outputs


# ---------------------------------------------------------------------------
# Case implementations.  Each returns a list of failure strings.
# ---------------------------------------------------------------------------

def _graphql_text(state):
    return json.dumps(state.graphql)


CASES = {}
PROJECTION = {}


def case(name):
    def deco(fn):
        CASES[name] = fn
        return fn
    return deco


@case("F2P-001")
def _f2p001(bin, s):
    s.pr_labels = [{"name": "stale", "node_id": "LA_stale"}]
    code, log, out = run_action(bin, s, STALE, sync=True, changed=["x.txt"])
    fails = []
    if code != 0:
        fails.append("exit=%d log=%s" % (code, log[-500:]))
    body = _graphql_text(s)
    if "removeLabelsFromLabelable" not in body:
        fails.append("no removeLabelsFromLabelable mutation: " + body[:400])
    if "PR_node_1" not in body or "LA_stale" not in body:
        fails.append("mutation missing node identifiers: " + body[:400])
    if s.rest_deletes:
        fails.append("used REST DELETE instead of GraphQL")
    if len(s.graphql) != 1:
        fails.append("expected one GraphQL mutation, got %d" % len(s.graphql))
    return fails


@case("F2P-002")
def _f2p002(bin, s):
    fails = []
    code, log, out = run_action(bin, s, BRANCH_RELEASE, head="release/1.2.3", changed=["x.txt"])
    if code != 0 or out.get("new-labels") != "rel":
        fails.append("semver branch pattern: exit=%d out=%s log=%s" % (code, out, log[-300:]))
    code, log, out = run_action(bin, s, BRANCH_RELEASE, head="main", changed=["x.txt"])
    if code != 0 or out.get("new-labels"):
        fails.append("semver pattern matched main: exit=%d out=%s" % (code, out))
    code, log, out = run_action(bin, s, BRANCH_ECMA, head="release/1.2.3", changed=["x.txt"])
    if code != 0 or out.get("new-labels") != "not-release":
        fails.append("ECMAScript negative lookahead not honoured: exit=%d out=%s log=%s" % (code, out, log[-300:]))
    # Without the Unicode flag, JavaScript regexps match UTF-16 code units.
    # An astral character occupies two units even though it is one Go rune.
    astral_branch = "😀"
    additions_before = len(s.added)
    code, log, out = run_action(bin, s, BRANCH_ONE_CODE_UNIT, head=astral_branch, changed=["x.txt"])
    if code != 0 or out.get("new-labels"):
        fails.append("^.$ matched a two-unit branch: exit=%d out=%s log=%s" % (code, out, log[-300:]))
    if len(s.added) != additions_before:
        fails.append("^.$ added a label for a two-unit branch: %s" % s.added[additions_before:])
    code, log, out = run_action(bin, s, BRANCH_TWO_CODE_UNITS, head=astral_branch, changed=["x.txt"])
    if code != 0 or out.get("new-labels") != "two-units":
        fails.append("^..$ did not match a two-unit branch: exit=%d out=%s log=%s" % (code, out, log[-300:]))
    # U+2028/U+2029 are legal branch characters, but are ECMAScript line
    # terminators. A .NET-style dot incorrectly matches these without dotAll.
    for separator in ("\u2028", "\u2029"):
        before = len(s.added)
        code, log, out = run_action(bin, s, BRANCH_ONE_CODE_UNIT, head=separator, changed=["x.txt"])
        if code != 0 or out.get("new-labels") or len(s.added) != before:
            fails.append("ECMAScript dot matched line separator U+%04X: exit=%d out=%s log=%s" %
                         (ord(separator), code, out, log[-300:]))
    return fails


def _pagination(bin, s, cfg=BASIC, match_file="docs/page2.md"):
    s.changed = ["src/a%d.go" % i for i in range(100)]
    s.file_page2 = [match_file]
    s.link_next = True
    code, log, out = run_action(bin, s, cfg, changed=s.changed)
    fails = []
    if code != 0:
        fails.append("exit=%d log=%s" % (code, log[-400:]))
    if out.get("new-labels") != "documentation":
        fails.append("label from second page not applied: out=%s" % out)
    if "2" not in s.file_pages:
        fails.append("page 2 never requested: pages=%s" % s.file_pages)
    return fails


@case("F2P-003")
def _f2p003(bin, s):
    return _pagination(bin, s)


@case("F2P-006")
def _f2p006(bin, s):
    return _pagination(bin, s)


@case("F2P-009")
def _f2p009(bin, s):
    return _pagination(bin, s)


@case("F2P-019")
def _f2p019(bin, s):
    return _pagination(bin, s)


@case("F2P-055")
def _f2p055(bin, s):
    return _pagination(bin, s)


def _recovery(bin, s, add_status, recovery, requested):
    cfg = "".join(
        "%s:\n  - changed-files:\n      - any-glob-to-any-file: ['docs/**']\n" % name
        for name in requested
    )
    s.add_status = add_status
    s.recovery_pages = recovery
    code, log, out = run_action(bin, s, cfg, changed=["docs/a.md"])
    fails = []
    if code != 0:
        fails.append("recovery failed: exit=%d log=%s" % (code, log[-400:]))
    if not s.added:
        fails.append("no label addition was attempted; matching behaviour is broken")
    elif sorted(s.added[-1]) != sorted(requested):
        fails.append("unexpected add payload: %s" % s.added)
    return fails


@case("F2P-004")
def _f2p004(bin, s):
    return _recovery(bin, s, 500, [["bug", "documentation"]], ["bug", "documentation"])


@case("F2P-014")
def _f2p014(bin, s):
    return _recovery(bin, s, 500, [["bug"]], ["bug"])


@case("F2P-015")
def _f2p015(bin, s):
    return _recovery(bin, s, 500, [["one", "two"], ["wanted"]], ["wanted"])


@case("F2P-017")
def _f2p017(bin, s):
    return _recovery(bin, s, 503, [["release"]], ["RELEASE"])


@case("F2P-020")
def _f2p020(bin, s):
    return _recovery(bin, s, 500, [["Bug"]], ["bug"])


@case("F2P-005")
def _f2p005(bin, s):
    code, log, out = run_action(bin, s, BASIC, changed=["docs/readme.md"])
    fails = []
    if code != 0:
        fails.append("exit=%d log=%s" % (code, log[-400:]))
    if out.get("new-labels") != "documentation":
        fails.append("new-labels=%r want documentation" % out.get("new-labels"))
    if out.get("all-labels") != "documentation":
        fails.append("all-labels=%r want documentation" % out.get("all-labels"))
    if s.added != [["documentation"]]:
        fails.append("label addition payload=%r want [[documentation]]" % s.added)
    return fails


@case("F2P-007")
def _f2p007(bin, s):
    code, log, out = run_action(bin, s, NEG_LIMIT, changed=["x.go"])
    fails = []
    if code == 0:
        fails.append("negative changed-files-labels-limit was accepted")
    if "changed-files-labels-limit" not in log:
        fails.append("error does not identify the offending option: %s" % log[-300:])
    return fails


@case("F2P-008")
def _f2p008(bin, s):
    code, log, out = run_action(bin, s, MAX_FILES, changed=["a.go", "b.go"])
    fails = []
    if code != 0:
        fails.append("exit=%d log=%s" % (code, log[-400:]))
    if s.added:
        fails.append("file labels added while max-files-changed exceeded: %s" % s.added)
    if out.get("new-labels"):
        fails.append("new-labels=%r want empty" % out.get("new-labels"))
    return fails


@case("F2P-010")
def _f2p010(bin, s):
    return _getcontent(bin, s)


def _getcontent(bin, s):
    code, log, out = run_action(bin, s, BASIC, local_config=False, changed=["docs/a.md"])
    fails = []
    if code != 0:
        fails.append("exit=%d log=%s" % (code, log[-400:]))
    if out.get("new-labels") != "documentation":
        fails.append("base64 configuration not decoded: out=%s" % out)
    return fails


@case("F2P-021")
def _f2p021(bin, s):
    return _getcontent(bin, s)


@case("F2P-022")
def _f2p022(bin, s):
    return _getcontent(bin, s)


@case("F2P-023")
def _f2p023(bin, s):
    return _getcontent(bin, s)


@case("F2P-056")
def _f2p056(bin, s):
    return _getcontent(bin, s)


@case("F2P-061")
def _f2p061(bin, s):
    return _getcontent(bin, s)


@case("F2P-062")
def _f2p062(bin, s):
    return _getcontent(bin, s)


@case("F2P-011")
def _f2p011(bin, s):
    s.pr_labels = [{"name": "stale", "node_id": "LA_a"}, {"name": "stale2", "node_id": "LA_b"}]
    code, log, out = run_action(bin, s, STALE2, sync=True, changed=["x.txt"])
    fails = []
    if code != 0:
        fails.append("exit=%d log=%s" % (code, log[-400:]))
    if s.rest_deletes:
        fails.append("used REST DELETE; expected GraphQL only")
    if len(s.graphql) != 1:
        fails.append("expected one GraphQL mutation, got %d" % len(s.graphql))
    return fails


@case("F2P-016")
def _f2p016(bin, s):
    s.pr_labels = [{"name": "stale", "node_id": "LA_stale_1"}, {"name": "stale2", "node_id": "LA_stale_2"}]
    code, log, out = run_action(bin, s, STALE2, sync=True, changed=["x.txt"])
    fails = []
    if code != 0:
        fails.append("exit=%d log=%s" % (code, log[-400:]))
    body = _graphql_text(s)
    for want in ("PR_node_1", "LA_stale_1", "LA_stale_2"):
        if want not in body:
            fails.append("mutation missing %s: %s" % (want, body[:400]))
    return fails


@case("F2P-012")
def _f2p012(bin, s):
    code, log, out = run_action(bin, s, UNKNOWN_KEY, changed=["x.go"])
    fails = []
    if code != 0:
        fails.append("unknown config option was not ignored: exit=%d log=%s" % (code, log[-400:]))
    if out.get("new-labels") != "mylabel":
        fails.append("valid label was not applied: out=%s" % out)
    return fails


@case("F2P-013")
def _f2p013(bin, s):
    code, log, out = run_action(bin, s, cap_config(150), changed=["x.go"])
    fails = []
    if code != 0:
        fails.append("exit=%d log=%s" % (code, log[-400:]))
    all_labels = (out.get("all-labels") or "").split(",")
    if len(all_labels) != 100:
        fails.append("all-labels has %d labels, want 100" % len(all_labels))
    if len(s.added) != 1 or len(s.added[0]) != 100:
        fails.append("expected one addition of 100 labels, got %d calls" % len(s.added))
    excess = sum(1 for i in range(100, 150) if ("label-%03d" % i) in log)
    if excess != 50:
        fails.append("warning listed %d excess labels, want 50" % excess)
    return fails


@case("F2P-018")
def _f2p018(bin, s):
    fails = []
    for invalid, character, escaped in (("12\x0734\n", "\x07", "\\x07"),
                                         ("12\r34", "\r", "\\x0d"),
                                         ("12\t34", "\t", "\\x09"),
                                         ("12\x1b[31m", "\x1b", "\\x1b")):
        _, log, _ = _expect(bin, s, fails, BASIC, pr_input=invalid)
        if escaped not in log or character in log:
            fails.append("non-printable pr-number was not escaped: %r %r" % (invalid, log[-300:]))
    return fails


@case("F2P-024")
def _f2p024(bin, s):
    fails = []
    code, log, out = run_action(bin, s, DOT, dot=False, changed=[".foo.pdf"])
    if code != 0 or out.get("new-labels"):
        fails.append("dot=false matched a hidden path: %s" % out)
    code, log, out = run_action(bin, s, DOT, dot=True, changed=[".foo.pdf"])
    if code != 0 or out.get("new-labels") != "touched":
        fails.append("dot=true did not match a hidden path: %s" % out)
    code, log, out = run_action(bin, s, NEGATION, dot=True, changed=["src/a.ts"])
    if code != 0 or out.get("new-labels") != "notdocs":
        fails.append("negated pattern did not match a non-docs path: %s" % out)
    code, log, out = run_action(bin, s, NEGATION, dot=True, changed=["docs/a.md"])
    if code != 0 or out.get("new-labels"):
        fails.append("negated pattern matched a docs path: %s" % out)
    return fails


def minimatch_cases():
    with open(os.path.join(os.path.dirname(__file__), "minimatch.json")) as handle:
        return json.load(handle)["cases"]


@case("minimatch-contract")
def _minimatch_contract(binary, state):
    _fresh(state)
    failures = _f2p024(binary, state)
    for scenario in minimatch_cases():
        config = json.dumps({"matched": [{"changed-files": [
            {"any-glob-to-any-file": scenario["pattern"]}]}]})
        start = len(failures)
        code, _, out = _expect(binary, state, failures, config,
                              "matched" if scenario["matches"] else "",
                              changed=[scenario["path"]], dot=scenario["dot"])
        # Record the result produced by the candidate, never the oracle value.
        PROJECTION[scenario["id"]] = code == 0 and out.get("new-labels", "") == "matched"
        failures[start:] = [scenario["id"] + ": " + failure for failure in failures[start:]]
    return failures


@case("F2P-068")
def _f2p068(bin, s):
    s.pr_labels = [{"name": "a", "node_id": "LA_a"}]
    code, log, out = run_action(bin, s, ORDERING, changed=["x.txt"])
    fails = []
    if code != 0:
        fails.append("exit=%d log=%s" % (code, log[-400:]))
    if out.get("all-labels") != "a,b,c":
        fails.append("all-labels=%r want a,b,c" % out.get("all-labels"))
    return fails


# ---------------------------------------------------------------------------
# Evaluator-owned ports of the source suites. These are executed independently
# of candidate test names; `go test -run MissingTest` is not a behaviour proof.
# They also preserve the public assertions when source tests migrate to Go.
# ---------------------------------------------------------------------------

def _fresh(state, **settings):
    api_url = state.api_url
    state.__init__()
    state.api_url = api_url
    for key, value in settings.items():
        setattr(state, key, value)


def _expect(binary, state, failures, config, want="", *, success=True, settings=None, **options):
    _fresh(state, **(settings or {}))
    options.setdefault("changed", ["docs/a.md"])
    code, log, out = run_action(binary, state, config, **options)
    if (code == 0) != success:
        failures.append("exit=%d expected success=%s config=%r log=%s" % (code, success, config, log[-800:]))
    if success and out.get("new-labels", "") != want:
        failures.append("new-labels=%r expected %r config=%r log=%s" %
                        (out.get("new-labels"), want, config, log[-800:]))
    expected_addition = want.split(",") if want else []
    if success and (bool(state.added) != bool(want) or any(call != expected_addition for call in state.added)):
        failures.append("addition payloads=%r expected %r" % (state.added, want))
    return code, log, out


def _branch_cases(binary, state, kinds):
    failures = []
    for kind in kinds:
        for group, patterns, branch, want in (
            ("any", ["^feature/"], "feature/x", True),
            ("any", ["^feature/"], "fix/x", False),
            ("any", ["^fix/", "x$"], "feature/x", True),
            ("any", ["^fix/", "z$"], "feature/x", False),
            ("all", ["^feature/", "x$"], "feature/x", True),
            ("all", ["^feature/", "z$"], "feature/x", False),
            ("all", ["^fix/", "z$"], "feature/x", False),
        ):
            config = json.dumps({"branch": [{group: [{kind + "-branch": patterns}]}]})
            _expect(binary, state, failures, config, "branch" if want else "", **{kind: branch})
    return failures


@case("source-branches")
@case("source-head-branches")
def _source_branches(binary, state):
    failures = _branch_cases(binary, state, ("head",))
    _expect(binary, state, failures, "bad: [{head-branch: '[bad'}]", success=False)
    return failures


@case("source-base-branches")
def _source_base_branches(binary, state):
    return _branch_cases(binary, state, ("base",))


@case("source-no-branches")
def _source_no_branches(binary, state):
    failures = []
    for head, base in (("feature/a", "main"), ("fix/b", "release")):
        _expect(binary, state, failures, BASIC, "documentation", head=head, base=base)
    return failures


@case("source-files")
def _source_files(binary, state):
    failures = []
    for mode, patterns, files, want in (
        ("any-glob-to-any-file", ["docs/**", "*.go"], ["docs/a.md", "x.txt"], True),
        ("any-glob-to-any-file", ["docs/**"], ["src/a.go"], False),
        ("any-glob-to-all-files", ["docs/**", "*.go"], ["a.go", "b.go"], True),
        ("any-glob-to-all-files", ["docs/**", "*.go"], ["docs/a.md", "b.go"], False),
        ("all-globs-to-any-file", ["src/**", "**/*.go"], ["src/a.go", "docs/a.md"], True),
        ("all-globs-to-any-file", ["src/**", "docs/**"], ["src/a.go", "docs/a.md"], False),
        ("all-globs-to-all-files", ["src/**", "**/*.go"], ["src/a.go", "src/b.go"], True),
        ("all-globs-to-all-files", ["src/**", "**/*.go"], ["src/a.go", "src/b.txt"], False),
    ):
        config = json.dumps({"files": [{"changed-files": [{mode: patterns}]}]})
        _expect(binary, state, failures, config, "files" if want else "", changed=files)
    _fresh(state)
    failures += _f2p024(binary, state)
    return failures


def grouping_cases():
    # This runtime-neutral fixture is also executed against the original
    # JavaScript during calibration. YAML items and normalized MatchConfigs
    # are different objects: plain options join the first implicit/explicit
    # any already produced by the baseline parser.
    path = os.path.join(os.path.dirname(__file__), "grouping.json")
    with open(path) as handle:
        return json.load(handle)["cases"]


@case("source-aggregation")
def _source_aggregation(binary, state):
    failures = []
    for scenario in grouping_cases():
        config = json.dumps({"both": scenario["rules"]})
        options = {"head": scenario["head"], "base": scenario["base"], "changed": scenario["files"]}
        matched = scenario["matches"]
        start = len(failures)
        code, _, initial = _expect(binary, state, failures, config, "both" if matched else "", **options)
        PROJECTION[scenario["id"]] = code == 0 and initial.get("new-labels", "") == "both"
        _, _, out = _expect(binary, state, failures, config, sync=True,
                            pr_labels=[{"name": "both", "node_id": "LA_both"},
                                       {"name": "external", "node_id": "LA_external"}], **options)
        mutation = _graphql_text(state)
        expected = "both,external" if matched else "external"
        if (out.get("all-labels") != expected or len(state.graphql) != int(not matched)
                or state.rest_deletes or "LA_external" in mutation
                or (not matched and ("removeLabelsFromLabelable" not in mutation or "LA_both" not in mutation))):
            failures.append("aggregation did not synchronize labels: %r %r" % (out, state.graphql))
        failures[start:] = [scenario["id"] + ": " + failure for failure in failures[start:]]
    return failures


@case("source-file-parser")
def _source_file_parser(binary, state):
    failures = []
    for block in ("[]", "[{}]"):
        _expect(binary, state, failures, "empty: [{changed-files: " + block + "}]")
    for block in ("broken", "[broken]", "[{unknown-glob: '*.go'}]"):
        _expect(binary, state, failures, "bad: [{changed-files: " + block + "}]", success=False)
    return failures


@case("source-config")
def _source_config(binary, state):
    return _config_limits(binary, state) + _config_unknown(binary, state)


@case("config-limit-validation")
def _config_limits(binary, state):
    failures = []
    for name in ("changed-files-labels-limit", "max-files-changed"):
        for value in ("2", "'2'", "' 2 '", "1", "'1'", "' 1 '", "0"):
            config = BASIC + "other:\n  - changed-files:\n      - any-glob-to-any-file: 'docs/**'\nbranch: [{head-branch: main}]\n"
            want = "documentation,other,branch" if "2" in value else "branch"
            _expect(binary, state, failures, name + ": " + value + "\n" + config, want,
                    changed=["docs/a.md", "docs/b.md"])
        for value in ("-1", "1.5", "'1.5'", "'2abc'", "true", "null", "[]"):
            _, log, _ = _expect(binary, state, failures, name + ": " + value + "\n" + BASIC, success=False)
            if name not in log:
                failures.append("validation error does not identify %s: %s" % (name, log[-600:]))
    return failures


@case("config-unknown")
def _config_unknown(binary, state):
    failures = []
    _, log, _ = _expect(binary, state, failures, UNKNOWN_KEY, "mylabel", changed=["x.go"])
    if "unknown-option" not in log or "::error" in log:
        failures.append("unknown key was not informationally ignored: " + log[-600:])
    return failures


@case("config-invalid")
def _config_invalid(binary, state):
    # Target postconditions required by instruction.md. They are deliberately
    # separate from P2P: the source silently accepts a mapping in changed-files.
    return (_source_config(binary, state) + _source_file_parser(binary, state)
            + _config_file_structure(binary, state) + _config_glob_types(binary, state))


def _invalid_file_blocks(binary, state, blocks):
    failures = []
    for block in blocks:
        _, log, _ = _expect(binary, state, failures, "bad: [{changed-files: " + block + "}]", success=False)
        if "changed-files" not in log and "glob" not in log.lower():
            failures.append("invalid changed-files error does not identify the offending block: " + log[-600:])
    return failures


@case("config-file-structure")
def _config_file_structure(binary, state):
    return _invalid_file_blocks(binary, state, ("{any-glob-to-any-file: '*.go'}",))


@case("config-glob-types")
def _config_glob_types(binary, state):
    return _invalid_file_blocks(binary, state, ("[{any-glob-to-any-file: 1}]",
                                              "[{any-glob-to-any-file: [1]}]"))


@case("config-loading")
def _config_loading(binary, state):
    failures = []
    for local in (False, True):
        _expect(binary, state, failures, BASIC, "documentation", local_config=local)
        fetched = any("/contents/" in path for method, path in state.requests)
        if fetched == local:
            failures.append("configuration must use local file when present and contents API otherwise")
    return failures


@case("source-inputs")
def _source_inputs(binary, state):
    failures = []
    for invalid in ("abc", "-1", "0", "12 34", "12abc", "12\r34", "12\t34", "12\x1b[31m"):
        _, log, _ = _expect(binary, state, failures, BASIC, pr_input=invalid)
        if "::warning" not in log or state.requests:
            failures.append("invalid PR input must warn and make no requests: %r %r %s" % (invalid, state.requests, log))
    for value in (None, "", " 123 \n\n"):
        _expect(binary, state, failures, BASIC, "documentation", pr_input=value)
        if not any("/pulls/123" in path for method, path in state.requests):
            failures.append("valid/event PR was not fetched")
    _expect(binary, state, failures, BASIC, "documentation", pr_input="123\ninvalid\n\n456")
    for number in (123, 456):
        if not any(path.split("?")[0].endswith("/pulls/%d" % number) for method, path in state.requests):
            failures.append("valid multiline PR %d was skipped" % number)
    return failures


@case("runtime-inputs")
def _runtime_inputs(binary, state):
    failures = []
    for dot in (False, True):
        _expect(binary, state, failures, DOT, "touched" if dot else "",
                dot=dot, changed=[".hidden.pdf"])
    return failures


@case("source-outputs")
def _source_outputs(binary, state):
    return _outputs(binary, state, (False, True))


@case("sync-labels")
def _sync_labels(binary, state):
    return _outputs(binary, state, (True,))


def _outputs(binary, state, modes):
    failures = []
    config = BASIC + "branch: [{head-branch: main}]\nstale: [{head-branch: never}]\n"
    existing = [{"name": "external", "node_id": "LA_external"}, {"name": "stale", "node_id": "LA_stale"}]
    for sync in modes:
        _, _, out = _expect(binary, state, failures, config, "documentation,branch", pr_labels=existing, sync=sync)
        expected = "external,documentation,branch" if sync else "external,stale,documentation,branch"
        if out.get("all-labels") != expected or len(state.graphql) != int(sync) or state.rest_deletes:
            failures.append("sync/outputs mismatch: %r %r" % (out, state.graphql))
        if sync and ("LA_stale" not in _graphql_text(state) or "LA_external" in _graphql_text(state)):
            failures.append("removed an unconfigured label or omitted the stale label")
    return failures


@case("source-limits")
def _source_limits(binary, state):
    return (_labels_limit(binary, state) + _max_files(binary, state)
            + _label_classification(binary, state))


@case("labels-limit")
def _labels_limit(binary, state):
    failures = []
    file_labels = BASIC + "other:\n  - changed-files:\n      - any-glob-to-any-file: 'docs/**'\nbranch: [{head-branch: main}]\n"
    for prefix, labels, want in (
        ("", [], "documentation,other,branch"),
        ("changed-files-labels-limit: 2\n", [], "documentation,other,branch"),
        ("changed-files-labels-limit: 1\n", [], "branch"),
        ("changed-files-labels-limit: 0\n", [], "branch"),
        ("changed-files-labels-limit: 1\n", [{"name": "documentation"}], "other,branch"),
        ("changed-files-labels-limit: 0\n", [{"name": "documentation"}], "branch"),
    ):
        _expect(binary, state, failures, prefix + file_labels, want, pr_labels=labels)
    return failures


@case("max-files")
def _max_files(binary, state):
    failures = []
    for max_files, want in ((1, "branch"), (2, "documentation,branch")):
        _expect(binary, state, failures, "max-files-changed: %d\n" % max_files + BASIC + "branch: [{head-branch: main}]\n",
                want, changed=["docs/a.md", "x.txt"])
    return failures


@case("label-classification")
def _label_classification(binary, state):
    failures = []
    mixed = "mixed: [{any: [{head-branch: main}, {changed-files: [{any-glob-to-any-file: 'docs/**'}]}]}]\nbranch: [{head-branch: main}]\n"
    _expect(binary, state, failures, "changed-files-labels-limit: 0\n" + mixed, "branch")
    _, _, out = _expect(binary, state, failures, "max-files-changed: 1\nchanged-files-labels-limit: 0\n" + mixed,
                        "branch", changed=["a.go", "b.go"], sync=True,
                        pr_labels=[{"name": "mixed", "node_id": "LA_mixed"}])
    if out.get("all-labels") != "mixed,branch" or state.graphql:
        failures.append("max-files must precede label limit and preserve mixed file labels")
    return failures


@case("source-skips")
def _source_skips(binary, state):
    failures = []
    for settings, options in (({"pull_status": 404}, {}), ({}, {"changed": []})):
        _, log, _ = _expect(binary, state, failures, BASIC, settings=settings, **options)
        if "::warning" not in log or state.added or state.graphql:
            failures.append("unfetchable/empty PR was not warned and skipped")
    return failures


@case("source-main")
def _source_main(binary, state):
    failures = (_source_outputs(binary, state) + _source_limits(binary, state)
                + _source_skips(binary, state) + _config_loading(binary, state))
    _fresh(state)
    failures += _f2p013(binary, state)
    return failures


@case("source-api")
@case("add-labels-recovery")
def _source_api(binary, state):
    failures = []
    for status, pages, verify_status, success in (
        (200, None, 200, True), (500, [["documentation"]], 200, True),
        (500, [["other"], ["documentation"]], 200, True),
        (500, [["other"]], 200, False), (403, None, 200, False),
        (500, None, 500, False),
    ):
        _expect(binary, state, failures, BASIC, "documentation" if success else "", success=success,
                settings={"add_status": status, "recovery_pages": pages, "verify_status": verify_status})
        verified = any(method == "GET" and "/labels" in path for method, path in state.requests)
        if verified != (status >= 500):
            failures.append("addition verification on wrong status: %d %r" % (status, state.requests))
    config = BASIC + "other:\n  - changed-files:\n      - any-glob-to-any-file: 'docs/**'\n"
    _expect(binary, state, failures, config, success=False,
            settings={"add_status": 500, "recovery_pages": [["documentation"]]})
    for probe in (_f2p004, _f2p014, _f2p015, _f2p017, _f2p020):
        _fresh(state)
        failures += probe(binary, state)
    return failures


@case("api-requests")
def _api_requests(binary, state):
    failures = []
    _expect(binary, state, failures, BASIC, "documentation", local_config=False)
    for method, suffix in (("GET", "/pulls/123"), ("GET", "/pulls/123/files"),
                           ("POST", "/issues/123/labels")):
        if not any(m == method and path.split("?")[0].endswith(suffix) for m, path in state.requests):
            failures.append("missing %s %s" % (method, suffix))
    if not any(m == "GET" and "/contents/" in path for m, path in state.requests):
        failures.append("missing repos.getContent request")
    return failures


@case("recovery-pagination")
def _recovery_pagination(binary, state):
    _fresh(state)
    return _f2p015(binary, state)


@case("recovery-verification-error")
def _recovery_verification_error(binary, state):
    failures = []
    _expect(binary, state, failures, BASIC, success=False,
            settings={"add_status": 500, "verify_status": 500})
    if not any(m == "GET" and "/labels" in path for m, path in state.requests):
        failures.append("server error was not verified")
    return failures


@case("bulk-sync")
def _bulk_sync(binary, state):
    failures = []
    for probe in (_f2p001, _f2p011, _f2p016):
        _fresh(state)
        failures += probe(binary, state)
    return failures


@case("source-errors")
@case("permission-errors")
def _source_errors(binary, state):
    failures = []
    for message, markers in (("Unauthorized", ("issues: write", "::error")),
                             ("Resource not accessible by integration", ("::warning", "::error"))):
        _, log, _ = _expect(binary, state, failures, BASIC, success=False,
                            settings={"add_status": 403, "add_message": message})
        if not all(marker in log for marker in markers):
            failures.append("missing permission/error diagnostics: " + log[-800:])
    return failures


@case("removal-error")
def _removal_error(binary, state):
    failures = []
    _, log, _ = _expect(binary, state, failures, STALE, success=False, sync=True,
                        pr_labels=[{"name": "stale", "node_id": "LA_stale"}], settings={"remove_error": True})
    if "stale" not in log or "123" not in log or len(state.graphql) != 1:
        failures.append("bulk removal error must name labels and PR: " + log[-800:])
    return failures


@case("config-missing")
def _config_missing(binary, state):
    failures = []
    _, log, _ = _expect(binary, state, failures, BASIC, success=False, local_config=False,
                        settings={"content_status": 404})
    if "::warning" not in log or "access" not in log.lower() or "::error" not in log:
        failures.append("missing-config warning/access/failure diagnostics: " + log[-800:])
    return failures


# ---------------------------------------------------------------------------
# Entrypoint.
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--runtime", choices=("target", "source"), default="target")
    args = parser.parse_args()
    if args.case not in CASES:
        print("unknown case %s" % args.case)
        return 2
    binary = (["node", os.path.join(REPO, "dist", "index.js")]
              if args.runtime == "source" else build_binary())
    state = State()
    server = start_server(state)
    state.api_url = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        fails = CASES[args.case](binary, state)
    finally:
        server.shutdown()
    report = ["%s %s" % ("FAIL" if fails else "PASS", args.case)]
    report += ["  - " + failure for failure in fails]
    report.append("requests: " + json.dumps(state.requests))
    text = "\n".join(report) + "\n"
    print(text)
    if PROJECTION:
        print("LABELER_PROJECTION " + json.dumps(PROJECTION, sort_keys=True))
    try:
        os.makedirs("/logs/blackbox", exist_ok=True)
        with open("/logs/blackbox/%s.txt" % args.case, "w") as handle:
            handle.write(text)
    except OSError:
        pass
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

"""Exercise the action through workflow inputs, REST and workflow outputs.

No candidate type, function, filename, test name, module path or glob library is
part of this adapter. The entire module (including tests, embed data and internal
packages) is staged. A Go -overlay redirects net/http's transport to a local
GitHub fixture; it changes no candidate source or business logic, and also works
when an implementation uses the fixed public GitHub API URL. Candidate tests
run separately, without that overlay. Every assertion below concerns the public
contract, including failure paths and request ordering.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import traceback
import urllib.parse
from launch import inspect_action

ASSETS = Path(__file__).resolve().parents[2]
IGNORED = {".git", ".gomodcache", ".gocache", "node_modules", "__pycache__"}


def run(argv, cwd, env, timeout=180):
    p = subprocess.run(["timeout", str(timeout), *map(str, argv)], cwd=cwd,
                       env=env, capture_output=True, text=True, timeout=timeout + 10)
    return p.returncode, p.stdout + p.stderr


def json_stream(text):
    decoder = json.JSONDecoder()
    while text.strip():
        obj, end = decoder.raw_decode(text.lstrip())
        yield obj
        text = text.lstrip()[end:]


def outputs(text):
    result = {}
    lines = iter(text.splitlines())
    for line in lines:
        if "<<" in line:
            key, delimiter = line.split("<<", 1)
            value = []
            for part in lines:
                if part == delimiter:
                    break
                value.append(part)
            result[key] = "\n".join(value)
        elif "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def action_main(repo, mains):
    """Choose a main package using the action's executable build identity."""
    if len(mains) == 1:
        return mains[0]
    inspected = inspect_action(repo, (repo / "action.yml").read_text())
    packages = set(inspected.get("go_packages", [])) if inspected["go"] else set()
    chosen = [p for p in mains if str(Path(p["Dir"]).relative_to(repo)) in packages]
    assert len(chosen) == 1, "Cannot identify the Go action executable"
    return chosen[0]


class Fixture:
    def __init__(self):
        self.state = {}
        self.calls = []
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                path = urllib.parse.urlsplit(self.path)
                try:
                    data = json.loads(raw) if raw else None
                except ValueError:
                    data = None
                row = {"method": self.command, "path": path.path,
                       "query": urllib.parse.parse_qs(path.query), "data": data,
                       "raw": raw, "headers": dict(self.headers)}
                fixture.calls.append(row)
                status, response = fixture.respond(row)
                encoded = json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded) if status != 204 else 0))
                self.end_headers()
                if status != 204:
                    self.wfile.write(encoded)

            do_GET = do_POST = do_PATCH = do_DELETE = handle_request

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.address = "127.0.0.1:%d" % self.server.server_port
        self.url = "http://" + self.address
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def release(self, **fields):
        result = {"id": 42, "tag_name": "ctx-tag", "draft": False, "prerelease": False,
                  "html_url": "https://example.test/release/42",
                  "upload_url": self.url + "/upload{?name,label}",
                  "tarball_url": None, "zipball_url": None}
        result.update(fields)
        return result

    def respond(self, row):
        state, path, method = self.state, row["path"], row["method"]
        error = state.get("error", {"message": "boom"})
        if path == "/upload" and method == "POST":
            attempts = sum(c["path"] == "/upload" for c in self.calls)
            statuses = state.get("upload_statuses", [201])
            status = statuses[min(attempts - 1, len(statuses) - 1)]
            name = row["query"].get("name", [""])[0]
            return status, ({"browser_download_url": "https://download.test/" + name}
                            if status < 300 else error)
        if path.endswith("/generate-notes") and method == "POST":
            return 200, {"body": "generated notes"}
        if "/releases/tags/" in path and method == "GET":
            status = state.get("lookup_status", 200)
            return status, self.release(**state.get("existing", {})) if status < 300 else error
        if re.search(r"/releases/assets/\d+$", path) and method == "DELETE":
            return state.get("delete_status", 204), error
        if re.search(r"/releases/\d+/assets$", path) and method == "GET":
            page = row["query"].get("page", ["1"])[0]
            return 200, state.get("assets", []) if page == "1" else []
        if path.endswith("/releases"):
            if method == "GET":
                return 200, state.get("drafts", [])
            if method == "POST":
                status = state.get("create_status", 201)
                return status, self.release(draft=(row["data"] or {}).get("draft", False)) if status < 300 else error
        if re.search(r"/releases/\d+$", path) and method == "PATCH":
            return 200, self.release(id=43 if state.get("publish") else 42)
        return 500, {"message": "Unexpected REST operation: " + method + " " + path}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class PublicBehavior:
    def __init__(self, repo, work):
        self.repo, self.work = Path(repo), Path(work)
        self.mod = self.work / "module"
        shutil.copytree(self.repo, self.mod, ignore=shutil.ignore_patterns(*IGNORED), symlinks=True)
        self.env = dict(os.environ)
        self.env.update(GOPROXY="off", GOTOOLCHAIN="local", GOWORK="off")
        self.fixture = Fixture()
        self.results = {}
        self.binary = self.work / "release-action"

    def build(self):
        assert (self.mod / "go.mod").is_file(), "Go module absent"
        rc, out = run(["go", "list", "-json", "./..."], self.mod, self.env)
        assert rc == 0, "Go package discovery failed: " + out[-1500:]
        mains = [p for p in json_stream(out) if p.get("Name") == "main"]
        # Follow the actual launcher/build mapping; a textual './.' search
        # cannot recognize a root command and can choose an unused executable.
        self.main = action_main(self.mod, mains)
        # Bind only the HTTP transport. Request methods, paths, query arguments,
        # authorization, bodies, output/error handling all remain candidate code.
        rc, goroot = run(["go", "env", "GOROOT"], self.mod, self.env)
        assert rc == 0, "Go toolchain unavailable"
        transport = Path(goroot.strip()) / "src/net/http/transport.go"
        original = transport.read_text()
        marker = "func (t *Transport) roundTrip(req *Request) (_ *Response, err error) {"
        if original.count(marker) != 1:
            raise RuntimeError("Go HTTP transport fixture is incompatible with the toolchain")
        redirect = '''
        if req.URL != nil {
            req = req.Clone(req.Context())
            fixtureURL := *req.URL
            fixtureURL.Scheme = "http"
            fixtureURL.Host = %s
            req.URL = &fixtureURL
            req.Host = ""
        }
''' % json.dumps(self.fixture.address)
        rewritten = self.work / "transport.go"
        rewritten.write_text(original.replace(marker, marker + redirect))
        overlay = self.work / "overlay.json"
        overlay.write_text(json.dumps({"Replace": {str(transport): str(rewritten)}}))
        rc, out = run(["go", "build", "-overlay", overlay, "-o", self.binary,
                       self.main["ImportPath"]], self.mod, self.env, 300)
        assert rc == 0, "Go executable build failed: " + out[-1500:]

    def invoke(self, inputs=None, state=None, env=None):
        self.fixture.state = state or {}
        self.fixture.calls = []
        output = self.work / "output"
        output.write_text("")
        e = {k: v for k, v in self.env.items() if not k.startswith("INPUT_")}
        e.update(GITHUB_REPOSITORY="ctx-owner/ctx-repo", GITHUB_REF="refs/tags/ctx-tag",
                 GITHUB_API_URL=self.fixture.url, GITHUB_OUTPUT=str(output),
                 HTTP_PROXY="", HTTPS_PROXY="", ALL_PROXY="", NO_PROXY="*",
                 HOME=str(self.work))
        values = {"token": "fixture-token", "skipIfReleaseExists": "false",
                  "replacesArtifacts": "false", "removeArtifacts": "false"}
        values.update(inputs or {})
        e.update({"INPUT_" + k.upper(): v for k, v in values.items()})
        e.update(env or {})
        rc, log = run([self.binary], self.mod, e, 30)
        assert rc != 124, "Action timed out"
        return rc, log, outputs(output.read_text()), list(self.fixture.calls)

    def success(self, inputs=None, state=None, env=None):
        result = self.invoke(inputs, state, env)
        assert result[0] == 0, "Action failed: " + result[1][-1200:]
        return result

    def requests(self, calls, method, suffix):
        return [c for c in calls if c["method"] == method and c["path"].endswith(suffix)]

    def created(self, inputs=None, state=None, env=None):
        _, _, out, calls = self.success(inputs, state, env)
        rows = self.requests(calls, "POST", "/releases")
        assert len(rows) == 1, "Expected one create-release request"
        return rows[0], out, calls

    def check(self, name, fn):
        try:
            fn()
            self.results[name] = {"status": "PASS", "detail": "public behavior verified"}
        except (AssertionError, ValueError, KeyError, IndexError) as e:
            self.results[name] = {"status": "FAIL", "detail": str(e) or traceback.format_exc(limit=2)}
        except subprocess.TimeoutExpired as e:
            self.results[name] = {"status": "INCONCLUSIVE", "detail": str(e)}

    def native_tests(self):
        # Preserve all test and fixture files. Require tests to actually run;
        # go test exit 0 with [no test files] is not migration evidence.
        files = [p for p in self.mod.rglob("*_test.go") if not set(p.relative_to(self.mod).parts) & IGNORED]
        assert files, "No migrated Go tests"
        assertion_files = [p for p in files if re.search(r"\b(?:\w+\.(?:Fatalf?|Errorf?|FailNow|Fail)|assert\.\w+|require\.\w+)\s*\(",
                                                         re.sub(r"//[^\n]*|/\*.*?\*/", "", p.read_text(), flags=re.S))]
        assert assertion_files, "Go tests have no failure assertions"
        rc, text = run(["go", "test", "-json", "-count=1", "./..."], self.mod, self.env, 300)
        rows = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
        ran = [r for r in rows if r.get("Action") == "pass" and r.get("Test")]
        skipped = [r for r in rows if r.get("Action") == "skip" and r.get("Test")]
        assert rc == 0 and ran, "Migrated assertion suite did not run and pass: " + text[-1500:]
        assert not skipped, "Migrated suite skipped tests"
        self.suite_evidence = {"executed_assertion_suite": True, "passed_tests": len(ran),
                               "assertion_files": [str(p.relative_to(self.mod)) for p in assertion_files]}

    def public_interface(self):
        baseline = json.loads((ASSETS / "fixtures/public-interface.json").read_text())["sections"]
        current = (self.mod / "action.yml").read_text()
        def sections(text):
            result = {}; active = None
            for line in text.splitlines():
                if line and not line.startswith((" ", "#")):
                    active = line.split(":", 1)[0]
                if active in {"inputs", "outputs", "branding"} and line.strip() and not line.lstrip().startswith("#"):
                    result.setdefault(active, []).append(line.strip().replace('"', "").replace("'", ""))
            return result
        assert sections(current) == baseline, "action.yml public definitions changed"

    def entry(self):
        text = (self.mod / "action.yml").read_text()
        block = re.search(r"(?ms)^runs:\s*\n(.*?)(?=^\S|\Z)", text)
        assert block, "Action has no runs entry"
        block = block[1]
        inspected = inspect_action(self.mod, text)
        assert not inspected["source_runtime_hits"], "Action launches source runtime: " + str(inspected["source_runtime_hits"])
        assert inspected["go"], "Action entry does not launch its Go implementation: " + str(inspected["unresolved"])
        if re.search(r"using:\s*['\"]?composite", block):
            # Composite actions do not receive automatic INPUT_* variables or
            # expose a step's outputs automatically, unlike Docker actions.
            inputs_block = re.search(r"(?ms)^inputs:\s*\n(.*?)(?=^\S|\Z)", text)
            input_names = re.findall(r"(?m)^  (\w+):", inputs_block[1] if inputs_block else "")
            all_inputs = re.search(r"(?i)tojson\s*\(\s*inputs\s*\)", block)
            for name in input_names:
                reference = r"inputs\s*(?:\.\s*" + re.escape(name) + r"\b|\[\s*['\"]" + re.escape(name) + r"['\"]\s*\])"
                assert all_inputs or re.search(reference, block), "Composite entry does not forward input " + name
            outputs_block = re.search(r"(?ms)^outputs:\s*\n(.*?)(?=^\S|\Z)", text)
            definitions = re.split(r"(?m)^  (\w+):\s*\n", outputs_block[1] if outputs_block else "")
            for index in range(1, len(definitions), 2):
                assert re.search(r"(?m)^    value:\s*\S", definitions[index+1]), "Composite output has no value mapping: " + definitions[index]
            return
        assert re.search(r"using:\s*['\"]?docker", block), "Action entry is not a Go launcher"

    def basic(self):
        row, out, calls = self.created()
        assert row["path"] == "/repos/ctx-owner/ctx-repo/releases"
        assert row["data"]["tag_name"] == "ctx-tag" and row["data"]["name"] == "ctx-tag"
        assert row["data"].get("body") == "" and row["data"].get("draft") is False
        assert row["data"].get("prerelease") is False
        assert out["id"] == "42" and out["html_url"] == "https://example.test/release/42"
        assert out["upload_url"] == self.fixture.url + "/upload{?name,label}"
        assert out["tarball_url"] == "" and out["zipball_url"] == ""
        assert json.loads(out["assets"]) == {}
        auth = {k.lower(): v for k, v in row["headers"].items()}.get("authorization", "")
        assert "fixture-token" in auth, "Required token not forwarded"
        assert not self.requests(calls, "GET", "/tags/ctx-tag"), "allowUpdates=false must create directly"

    def tags(self):
        row, _, _ = self.created({"tag": "  explicit-tag  "})
        assert row["data"]["tag_name"] == "explicit-tag"
        rc, log, _, calls = self.invoke(env={"GITHUB_REF": "refs/heads/main"})
        assert rc != 0 and "No tag found in ref or input!" in log and not calls
        rc, log, _, calls = self.invoke(env={"GITHUB_REF": "refs/tags/"})
        assert rc != 0 and "No tag found in ref or input!" in log and not calls
        rc, _, _, calls = self.invoke({"token": ""})
        assert rc != 0 and not calls, "Missing required token must fail before REST"

    def strings(self):
        row, _, _ = self.created({"name": "  title  ", "body": "  body  ", "owner": " owner ",
                                  "repo": " repo ", "commit": " abc ", "discussionCategory": " news "})
        assert row["path"] == "/repos/owner/repo/releases"
        for key, value in {"name": "title", "body": "body", "target_commitish": "abc", "discussion_category_name": "news"}.items():
            assert row["data"][key] == value, key
        body = self.work / "body.txt"; body.write_text("body file\n")
        row, _, _ = self.created({"bodyFile": str(body)})
        assert row["data"]["body"] == "body file\n"
        row, _, _ = self.created({"bodyFile": str(body), "body": "priority"})
        assert row["data"]["body"] == "priority"
        for value in ("legacy", "true", "false", "invalid"):
            row, _, _ = self.created({"makeLatest": value})
            assert row["data"].get("make_latest") == (value if value != "invalid" else None)
        row, _, _ = self.created({"omitName": "true", "omitBody": "true"})
        assert "name" not in row["data"] and "body" not in row["data"]

    def booleans(self):
        for value, expected in (("true", True), ("TRUE", False)):
            row, _, calls = self.created({"draft": value, "prerelease": value})
            assert row["data"]["draft"] is expected and row["data"]["prerelease"] is expected
            _, _, _, calls = self.success({"allowUpdates": value})
            assert bool(self.requests(calls, "PATCH", "/releases/42")) is expected

    def update(self):
        _, _, _, calls = self.success({"allowUpdates": "true", "tag": "v-update", "name": "new", "body": "new body", "commit": "abc"})
        rows = self.requests(calls, "PATCH", "/releases/42")
        assert len(rows) == 1 and rows[0]["data"]["tag_name"] == "v-update"
        assert rows[0]["data"]["name"] == "new" and rows[0]["data"]["body"] == "new body"
        assert rows[0]["data"]["target_commitish"] == "abc"
        _, _, _, calls = self.success({"allowUpdates": "true", "body": "discard", "name": "discard", "draft": "true", "prerelease": "true",
                                      "omitBodyDuringUpdate": "true", "omitNameDuringUpdate": "true",
                                      "omitDraftDuringUpdate": "true", "omitPrereleaseDuringUpdate": "true"})
        data = self.requests(calls, "PATCH", "/releases/42")[0]["data"]
        assert not {"body", "name", "draft", "prerelease"} & data.keys()
        for existing, should_fail in (({"draft": False, "prerelease": False}, True), ({"draft": True}, False), ({"prerelease": True}, False)):
            rc, _, _, calls = self.invoke({"allowUpdates": "true", "updateOnlyUnreleased": "true"}, {"existing": existing})
            assert (rc != 0) == should_fail
            assert bool(self.requests(calls, "PATCH", "/releases/42")) != should_fail

    def missing(self):
        _, _, _, calls = self.success({"allowUpdates": "true"}, {"lookup_status": 404})
        assert self.requests(calls, "GET", "/releases") and self.requests(calls, "POST", "/releases")
        draft = self.fixture.release(id=51, tag_name="ctx-tag", draft=True)
        _, _, _, calls = self.success({"allowUpdates": "true"}, {"lookup_status": 404, "drafts": [self.fixture.release(id=52, tag_name="other", draft=True), draft]})
        assert self.requests(calls, "PATCH", "/releases/51") and not self.requests(calls, "POST", "/releases")
        rc, log, _, calls = self.invoke({"allowUpdates": "true"}, {"lookup_status": 500})
        assert rc != 0 and "Error 500: boom" in log
        assert not self.requests(calls, "POST", "/releases") and not self.requests(calls, "GET", "/releases")

    def skip(self):
        for value in ("true", "True", "TRUE"):
            _, log, _, calls = self.success({"skipIfReleaseExists": value})
            assert "::notice::" in log and len(calls) == 1
        for value in ("false", "False", "FALSE"):
            self.created({"skipIfReleaseExists": value})
        rc, _, _, calls = self.invoke({"skipIfReleaseExists": "yes"})
        assert rc != 0 and not calls
        # The contract specifies non-draft success and lookup failure. It does
        # not prescribe the outcome of a successful lookup returning a draft.
        self.created({"skipIfReleaseExists": "true"}, {"lookup_status": 500})

    def skip_files(self):
        # A published release must short-circuit work that would otherwise
        # fail, including body-file reads and strict artifact expansion.
        missing = str(self.work / "skip-missing-file")
        pending = ({"bodyFile": missing},
                   {"artifacts": missing, "artifactErrorsFailBuild": "true"},
                   {"artifact": missing, "artifactErrorsFailBuild": "true"})
        for values in pending:
            inputs = {"skipIfReleaseExists": "true", **values}
            _, log, out, calls = self.success(inputs)
            assert "::notice::" in log and "::warning::" not in log
            assert not out, "Skipped action must not report release outputs"
            lookups = self.requests(calls, "GET", "/tags/ctx-tag")
            assert len(calls) == len(lookups) == 1, "Skip must precede remaining release/artifact work"
            # A failed skip lookup must continue, so these invalid file
            # inputs must still fail rather than being silently ignored.
            rc, _, _, calls = self.invoke(inputs, {"lookup_status": 500})
            assert rc != 0, "Lookup failure must continue processing file inputs"
            assert self.requests(calls, "GET", "/tags/ctx-tag"), "File work ran before the skip lookup"

    def omitted_body_files(self):
        missing = str(self.work / "omitted-missing-body")
        row, _, _ = self.created({"omitBody": "true", "bodyFile": missing})
        assert "body" not in row["data"], "Omitted create body must not read bodyFile"
        for flag in ("omitBody", "omitBodyDuringUpdate"):
            inputs = {"allowUpdates": "true", flag: "true", "bodyFile": missing}
            if flag == "omitBodyDuringUpdate":
                inputs["generateReleaseNotes"] = "true"
            _, _, _, calls = self.success(inputs)
            data = self.requests(calls, "PATCH", "/releases/42")[0]["data"]
            assert "body" not in data
            assert not self.requests(calls, "POST", "/generate-notes")
        draft = self.fixture.release(id=51, tag_name="ctx-tag", draft=True)
        inputs = {"allowUpdates": "true", "omitBodyDuringUpdate": "true", "bodyFile": missing}
        _, _, _, calls = self.success(inputs, {"lookup_status": 404, "drafts": [draft]})
        assert "body" not in self.requests(calls, "PATCH", "/releases/51")[0]["data"]
        # The same flag cannot suppress a body-file error when the 404
        # fallback actually creates a new release.
        rc, _, _, calls = self.invoke(inputs, {"lookup_status": 404})
        assert rc != 0, "Create fallback must still resolve its bodyFile"
        assert self.requests(calls, "GET", "/releases"), "Body resolution preceded release selection"
        rc, _, _, _ = self.invoke({"bodyFile": missing})
        assert rc != 0, "Required body-file reads must still fail when the file is absent"

    def notes(self):
        for body, expected in (("B", "B\ngenerated notes"), ("", "generated notes")):
            row, _, calls = self.created({"generateReleaseNotes": "true", "body": body,
                                          "generateReleaseNotesPreviousTag": "prev", "commit": "sha"})
            assert row["data"]["body"] == expected
            generated = self.requests(calls, "POST", "/generate-notes")
            assert len(generated) == 1 and generated[0]["data"] == {"tag_name": "ctx-tag", "previous_tag_name": "prev", "target_commitish": "sha"}
        _, _, _, calls = self.success({"allowUpdates": "true", "generateReleaseNotes": "true", "omitBodyDuringUpdate": "true"})
        assert not self.requests(calls, "POST", "/generate-notes")
        assert "body" not in self.requests(calls, "PATCH", "/releases/42")[0]["data"]

    def artifact_files(self):
        directory = self.work / "artifacts"; directory.mkdir(exist_ok=True)
        (directory / "a.bin").write_bytes(b"AAAA")
        (directory / "b.bin").write_bytes(b"BBBBB")
        (directory / "nested").mkdir(exist_ok=True)
        (directory / "nested/c.bin").write_bytes(b"CCC")
        return directory

    def uploaded(self, inputs, state=None):
        _, log, out, calls = self.success(inputs, state)
        uploads = self.requests(calls, "POST", "/upload")
        return uploads, json.loads(out["assets"]), calls, log

    def artifacts(self):
        directory = self.artifact_files()
        for pattern in (str(directory / "a.bin") + ",\n " + str(directory / "b.bin"),
                        str(directory / "*.bin"), "~/artifacts/*.bin"):
            uploads, assets, _, _ = self.uploaded({"artifacts": pattern})
            assert sorted(c["query"]["name"][0] for c in uploads) == ["a.bin", "b.bin"]
            assert assets == {"a.bin": "https://download.test/a.bin", "b.bin": "https://download.test/b.bin"}
            for row in uploads:
                name = row["query"]["name"][0]
                assert row["raw"] == (directory / name).read_bytes()
                headers = {k.lower(): v for k, v in row["headers"].items()}
                assert int(headers["content-length"]) == len(row["raw"])
                assert headers["content-type"] == "raw"
        uploads, _, _, _ = self.uploaded({"artifacts": str(directory / "**/*.bin")})
        assert sorted(c["query"]["name"][0] for c in uploads) == ["a.bin", "b.bin", "c.bin"]
        uploads, _, _, _ = self.uploaded({"artifact": str(directory / "a.bin"), "artifactContentType": "application/octet-stream"})
        assert len(uploads) == 1 and uploads[0]["headers"].get("Content-Type") == "application/octet-stream"
        uploads, _, _, _ = self.uploaded({"artifact": str(directory / "a.bin"), "artifacts": str(directory / "b.bin")})
        assert [c["query"]["name"][0] for c in uploads] == ["b.bin"]
        # Relative paths must be relative to the candidate module's workdir.
        relative = self.mod / "relative.bin"; relative.write_bytes(b"relative")
        try:
            uploads, _, _, _ = self.uploaded({"artifacts": "relative.bin"})
            assert len(uploads) == 1 and uploads[0]["raw"] == b"relative"
        finally:
            relative.unlink()

    def empty_artifacts(self):
        # An os.File request body with ContentLength=0 is unknown-length to
        # net/http unless the client explicitly marks it as an empty body.
        file = self.work / "empty.bin"
        file.write_bytes(b"")
        uploads, assets, _, _ = self.uploaded({"artifacts": str(file)})
        assert len(uploads) == 1, "Empty artifact must be uploaded exactly once"
        row = uploads[0]
        assert row["query"].get("name") == [file.name], "Empty artifact name changed"
        assert row["raw"] == b"", "Empty artifact upload contains data"
        headers = {k.lower(): v for k, v in row["headers"].items()}
        assert headers.get("content-length") == "0", "Empty artifact must send Content-Length: 0"
        assert not headers.get("transfer-encoding"), "Empty artifact must not use unknown-length chunked transfer"
        assert assets == {file.name: "https://download.test/" + file.name}, "Empty artifact missing from assets output"

    # Frozen minimatch POSIX named-class expectations (glob@13.0.6 ->
    # minimatch@10.2.5). Each entry maps a pattern to the set of filenames it
    # must match; the reference was captured by running the pre-migration
    # implementation. POSIX classes translate to Unicode property escapes, so
    # [[:alpha:]] matches a Unicode letter such as U+00E9.
    POSIX_CLASS_CASES = (
        ("[[:digit:]].zip", ("1.zip",)),
        ("[[:alpha:]].zip", ("a.zip", "Z.zip", "é.zip")),
        ("[[:alnum:]].zip", ("1.zip", "a.zip", "Z.zip", "é.zip")),
        ("[![:digit:]].zip", ("a.zip", "Z.zip", "é.zip")),
        ("[^[:digit:]].zip", ("a.zip", "Z.zip", "é.zip")),
        ("[![:alpha:]].zip", ("1.zip",)),
        ("[[:upper:]].zip", ("Z.zip",)),
        ("[[:graph:]].zip", ("1.zip", "a.zip", "Z.zip", "é.zip")),
        # In minimatch outer negation applies to the ordinary and internally
        # complemented class buckets separately, then joins them with OR.
        ("[![:graph:]a].zip", ("1.zip", "Z.zip", "é.zip")),
        ("[^[:graph:]a].zip", ("1.zip", "Z.zip", "é.zip")),
        # Descending ranges are discarded without discarding other members.
        ("[z-aa].zip", ("a.zip",)),
        ("[z-a].zip", ()),
        # minimatch v10.2.5 translates [:print:] to the bare \p{C} property
        # without the negated flag, so its observable behaviour is
        # "Other/control" and matches no ordinary filename. Preserve it.
        ("[[:print:]].zip", ()),
    )

    # glob/minimatch treats unfinished and empty bracket expressions as
    # literals. Go glob libraries can reject them as malformed instead.
    LITERAL_BRACKET_CASES = (
        ("literal[.zip", ("literal[.zip",)),
        ("[].zip", ("[].zip",)),
    )

    # Captured from the bundled glob@13.0.6 implementation. In particular a
    # negative extglob excludes its alternative together with the remaining
    # suffix, rather than merely complementing possible substring lengths.
    EXTENDED_GLOB_CASES = (
        ("@(a|b).zip", ("a.zip", "b.zip")),
        ("+(a|b).zip", ("a.zip", "ab.zip", "b.zip")),
        ("?(a|b).zip", ("a.zip", "b.zip")),
        ("*(a|b).zip", ("a.zip", "ab.zip", "b.zip")),
        ("!(a)*.zip", ("1.zip", "2.zip", "3.zip", "b.zip", "é.zip")),
        # Negative lookaheads inside a group include the outer .zip suffix.
        # Testing only a top-level !(a)* misses implementations that upload
        # ab.zip for these nested forms.
        ("@(a!(b)|b).zip", ("a.zip", "b.zip")),
        ("?(a!(b)|b).zip", ("a.zip", "b.zip")),
        ("+(a!(b)|b).zip", ("a.zip", "b.zip")),
        # Preserve minimatch's non-empty wildcard for a negative group whose
        # final alternative is empty or ends in another extglob.
        ("!(a|).zip", ("1.zip", "2.zip", "3.zip", "a.zip", "ab.zip", "b.zip", "é.zip")),
        ("!(@(a|b)).zip", ("1.zip", "2.zip", "3.zip", "a.zip", "ab.zip", "b.zip", "é.zip")),
        ("{1..3}.zip", ("1.zip", "2.zip", "3.zip")),
        ("@(.hidden|a).zip", (".hidden.zip", "a.zip")),
        ("[[:graph:]a].zip", ("1.zip", "2.zip", "3.zip", "a.zip", "b.zip", "é.zip")),
    )

    # Escapes are part of the existing glob syntax. Structural scans must
    # preserve them until literal matching, including inside extglob groups.
    ESCAPED_GLOB_CASES = (
        (r"\{1..2}.zip", ("{1..2}.zip",)),
        (r"{1..2\}.zip", ("{1..2}.zip",)),
        (r"\{1..2\}{3..4}.zip", ("{1..2}3.zip", "{1..2}4.zip")),
        # Captured from FileArtifactGlobber/glob@13.0.6, not a standalone
        # minimatch approximation: the glob traversal yields b.zip and c.zip.
        (r"@(a\|b|c).zip", ("b.zip", "c.zip")),
        (r"@(a\)|c).zip", ("a).zip", "c.zip")),
        (r"@(a\(b|c).zip", ("a(b.zip", "c.zip")),
    )

    # Frozen glob@13.0.6 brace-expansion behavior: ${...} stays literal and
    # permits a later range; a non-range outer group keeps the whole pattern.
    LITERAL_BRACE_CASES = (
        ("${1..2}.zip", ("${1..2}.zip",)),
        ("${{1..2}}.zip", ("${{1..2}}.zip",)),
        ("{{1..2}}.zip", ("{{1..2}}.zip",)),
        ("${a}{1..2}.zip", ("${a}1.zip", "${a}2.zip")),
        ("{{1..2}}{1..2}.zip", ("{{1..2}}{1..2}.zip",)),
    )

    def glob_classes(self):
        directory = self.work / "classes"; directory.mkdir(exist_ok=True)
        for name in ("1.zip", "a.zip", "é.zip", "Z.zip", "literal[.zip", "[].zip"):
            (directory / name).write_bytes(name.encode("utf-8"))
        for pattern, expected in self.POSIX_CLASS_CASES + self.LITERAL_BRACKET_CASES:
            inputs = {"artifacts": str(directory / pattern)}
            if (pattern, expected) in self.LITERAL_BRACKET_CASES:
                inputs["artifactErrorsFailBuild"] = "true"
            uploads, assets, _, _ = self.uploaded(inputs)
            names = sorted(upload["query"]["name"][0] for upload in uploads)
            assert names == sorted(expected), "%s matched %s, want %s" % (pattern, names, sorted(expected))
            assert sorted(assets) == sorted(expected), "%s assets %s" % (pattern, sorted(assets))
        extended = self.work / "extended-classes"; extended.mkdir(exist_ok=True)
        for name in ("1.zip", "2.zip", "3.zip", "a.zip", "ab.zip", "b.zip", "é.zip", ".hidden.zip"):
            (extended / name).write_bytes(name.encode("utf-8"))
        for pattern, expected in self.EXTENDED_GLOB_CASES:
            uploads, assets, _, _ = self.uploaded({"artifacts": str(extended / pattern)})
            names = sorted(upload["query"]["name"][0] for upload in uploads)
            assert names == sorted(expected), "%s matched %s, want %s" % (pattern, names, sorted(expected))
            assert sorted(assets) == sorted(expected), "%s assets %s" % (pattern, sorted(assets))
        escaped = self.work / "escaped-classes"; escaped.mkdir(exist_ok=True)
        # Include decoys so accidental expansion/alternative splitting is
        # caught even when it successfully uploads a different existing file.
        for name in ("{1..2}.zip", "1.zip", "2.zip", "{1..2}3.zip", "{1..2}4.zip",
                     "a|b.zip", "a).zip", "a(b.zip", "b.zip", "c.zip",
                     "${1..2}.zip", "$1.zip", "$2.zip", "${{1..2}}.zip", "${1}.zip", "${2}.zip",
                     "{{1..2}}.zip", "{1}.zip", "{2}.zip", "${a}1.zip", "${a}2.zip",
                     "{{1..2}}{1..2}.zip", "{{1..2}}1.zip", "{{1..2}}2.zip"):
            (escaped / name).write_bytes(name.encode("utf-8"))
        for pattern, expected in self.ESCAPED_GLOB_CASES + self.LITERAL_BRACE_CASES:
            uploads, assets, _, _ = self.uploaded({"artifacts": str(escaped / pattern),
                                                 "artifactErrorsFailBuild": "true"})
            names = sorted(upload["query"]["name"][0] for upload in uploads)
            assert names == sorted(expected), "%s matched %s, want %s" % (pattern, names, sorted(expected))
            assert sorted(assets) == sorted(expected), "%s assets %s" % (pattern, sorted(assets))

    def invalid_artifacts(self):
        directory = self.artifact_files()
        bracket_directory = directory / "nested["
        bracket_directory.mkdir(exist_ok=True)
        for pattern in (str(directory / "absent*"), str(directory / "nested"),
                        str(directory / "absent[.zip"), str(bracket_directory)):
            uploads, _, _, log = self.uploaded({"artifacts": pattern})
            assert not uploads and "::warning::" in log
            rc, _, _, calls = self.invoke({"artifacts": pattern, "artifactErrorsFailBuild": "true"})
            assert rc != 0 and not self.requests(calls, "POST", "/upload")

    def replace(self):
        directory = self.artifact_files()
        assets = [{"id": 10, "name": "a.bin"}, {"id": 11, "name": "other"}]
        for flag, expected in (("replacesArtifacts", [10]), ("removeArtifacts", [10, 11])):
            _, _, calls, _ = self.uploaded({"artifacts": str(directory / "a.bin"), flag: "true"}, {"assets": assets})
            deleted = [int(c["path"].rsplit("/", 1)[1]) for c in calls if c["method"] == "DELETE"]
            assert deleted == expected
            assert max(i for i, c in enumerate(calls) if c["method"] == "DELETE") < next(i for i, c in enumerate(calls) if c["path"] == "/upload")
        _, _, calls, _ = self.uploaded({"artifacts": str(directory / "a.bin")}, {"assets": assets})
        assert not any(c["method"] == "DELETE" for c in calls)
        rc, _, _, calls = self.invoke({"artifacts": str(directory / "a.bin"), "replacesArtifacts": "true", "artifactErrorsFailBuild": "true"}, {"assets": assets, "delete_status": 403})
        assert rc != 0 and not self.requests(calls, "POST", "/upload")

    def retries(self):
        file = self.artifact_files() / "a.bin"
        for status, attempts in ((500, 4), (400, 1)):
            uploads, assets, _, log = self.uploaded({"artifacts": str(file)}, {"upload_statuses": [status]})
            assert len(uploads) == attempts and assets == {} and "::warning::" in log
            rc, _, _, calls = self.invoke({"artifacts": str(file), "artifactErrorsFailBuild": "true"}, {"upload_statuses": [status]})
            assert rc != 0 and len(self.requests(calls, "POST", "/upload")) == attempts
        uploads, assets, _, _ = self.uploaded({"artifacts": str(file)}, {"upload_statuses": [500, 201]})
        assert len(uploads) == 2 and assets == {"a.bin": "https://download.test/a.bin"}

    def immutable(self):
        file = self.artifact_files() / "a.bin"
        _, _, out, calls = self.success({"immutableCreate": "true", "artifacts": str(file), "body": "B", "commit": "abc"}, {"publish": True})
        create = self.requests(calls, "POST", "/releases")[0]
        publish = self.requests(calls, "PATCH", "/releases/42")[0]
        assert create["data"]["draft"] is True and publish["data"]["draft"] is False
        assert "body" not in publish["data"] and "target_commitish" not in publish["data"]
        assert calls.index(create) < next(i for i, c in enumerate(calls) if c["path"] == "/upload") < calls.index(publish)
        assert out["id"] == "43", "Outputs must use publish response"
        _, _, _, calls = self.success({"immutableCreate": "true", "draft": "true"})
        assert not self.requests(calls, "PATCH", "/releases/42")

    def errors(self):
        details = [{"code": "missing", "resource": "Release"},
                   {"code": "missing_field", "field": "tag", "resource": "Release"},
                   {"code": "invalid", "field": "name", "resource": "Release"},
                   {"code": "already_exists", "resource": "Release"},
                   {"code": "custom", "message": "detail", "documentation_url": "https://docs.test"}]
        for status in (404, 422):
            rc, log, _, _ = self.invoke(state={"create_status": status, "error": {"message": "bad release", "errors": details}})
            assert rc != 0
            for fragment in ("Error %d: bad release" % status, "Errors:", "- Release does not exist.",
                             "- The tag field on Release is missing.", "- The name field on Release is an invalid format.",
                             "- Release already exists.", "- detail", "Please see https://docs.test."):
                assert fragment in log, "Missing GitHub error text: " + fragment
            advice = "Make sure your github token has access to the repo and has permission to author releases"
            assert (advice in log) == (status == 404)

    def execute(self):
        try:
            self.build()
        except AssertionError as e:
            return {name: {"status": "FAIL", "detail": str(e)} for name in SCENARIOS}
        for name in SCENARIOS:
            self.check(name, getattr(self, name))
        if self.results["native_tests"]["status"] == "PASS":
            self.results["native_tests"].update(self.suite_evidence)
        return self.results


SCENARIOS = ("entry", "public_interface", "basic", "tags", "strings", "booleans", "update", "missing", "skip",
             "skip_files", "omitted_body_files", "notes", "artifacts", "empty_artifacts", "glob_classes", "invalid_artifacts", "replace", "retries", "immutable", "errors", "native_tests")

# Explicit behavior ownership replaces oracle function/test-name signatures.
F2P = {
    1: ["entry"], 2: ["notes"], 3: ["native_tests"], 4: ["skip"], 5: ["notes"], 6: ["basic"],
    7: ["replace"], 8: ["invalid_artifacts"], 9: ["basic"], 10: ["artifacts", "glob_classes"], 11: ["strings"],
    12: ["basic"], 13: ["immutable"], 14: ["basic"], 15: ["strings"], 16: ["native_tests"],
    17: ["skip"], 18: ["tags"], 19: ["immutable"], 20: ["errors"], 21: ["skip"], 22: ["strings"],
    23: ["errors"], 24: ["missing"], 25: ["entry"], 26: ["artifacts", "glob_classes"], 27: ["skip"], 28: ["missing"],
    29: ["skip"], 30: ["notes"], 31: ["entry", "basic"], 32: ["entry", "public_interface", "missing", "replace"],
    33: ["entry"], 34: ["invalid_artifacts"], 35: ["skip"], 36: ["basic"], 37: ["artifacts", "glob_classes"],
    38: ["update"], 39: ["basic"], 40: ["invalid_artifacts"], 41: ["tags"], 42: ["artifacts", "glob_classes"],
    43: ["immutable"], 44: ["errors"], 45: ["artifacts", "glob_classes"], 46: ["strings"], 47: ["skip"],
    48: ["errors", "missing"], 49: ["booleans"], 50: ["basic"], 51: ["notes", "omitted_body_files"], 52: ["artifacts", "glob_classes"],
    53: ["artifacts", "glob_classes"], 54: ["skip", "skip_files"], 55: ["replace"], 56: ["invalid_artifacts"], 57: ["artifacts", "glob_classes"],
    58: ["basic"], 59: ["retries"], 60: ["native_tests"], 61: ["basic"], 62: ["immutable"],
    63: ["basic", "artifacts", "empty_artifacts", "glob_classes"], 64: ["skip"], 65: ["replace"], 66: ["tags"], 67: ["errors"],
    68: ["basic"], 69: ["errors"], 70: ["errors"], 71: ["basic", "entry"], 72: ["basic", "entry"],
}
P2P = {
    1: ["basic", "update", "missing", "notes", "immutable", "skip"], 2: ["artifacts", "glob_classes", "retries", "replace"],
    3: ["artifacts", "glob_classes"], 4: ["strings"], 5: ["update"], 6: ["update"], 7: ["artifacts", "glob_classes"], 8: ["tags"],
    9: ["public_interface"], 10: ["artifacts", "glob_classes", "invalid_artifacts"], 11: ["errors"], 12: ["tags"],
    13: ["basic", "artifacts", "empty_artifacts"], 14: ["retries"], 15: ["artifacts", "glob_classes"], 16: ["public_interface"],
    17: ["replace"], 18: ["skip", "skip_files"], 19: ["booleans"], 20: ["strings"], 21: ["booleans"],
    22: ["public_interface", "basic", "missing", "replace"], 23: ["basic", "update", "missing", "notes", "immutable", "skip"],
    24: ["public_interface", "entry"], 25: ["invalid_artifacts"], 26: ["basic"], 27: ["update"],
    28: ["artifacts", "glob_classes"], 29: ["missing"], 30: ["retries"], 31: ["tags"], 32: ["skip"],
    33: ["booleans"], 34: ["immutable"], 35: ["update"], 36: ["notes", "omitted_body_files"], 37: ["strings"],
    38: ["native_tests"], 39: ["update"], 40: ["replace"],
}

# CONTRACT is the public/observable contract only: action.yml definitions, the
# GitHub REST route/field mapping, input parsing semantics, artifact
# glob/path handling, outputs and error formatting. It is evaluated through the
# same black-box adapter as P2P/F2P (the real candidate executable driven
# against a mock REST server), so it never depends on private Go identifiers
# or on the candidate layout being a flat top-level package.
CONTRACT = {
    1: ["basic"], 2: ["basic"], 3: ["public_interface"], 4: ["public_interface"],
    5: ["basic", "update", "notes", "errors"], 6: ["tags"], 7: ["tags"],
    8: ["artifacts", "glob_classes"], 9: ["strings"], 10: ["strings"],
    11: ["booleans"], 12: ["update"], 13: ["missing"], 14: ["skip", "skip_files"],
    15: ["update"], 16: ["artifacts"], 17: ["artifacts", "invalid_artifacts", "glob_classes"],
    18: ["artifacts", "empty_artifacts"], 19: ["replace"], 20: ["retries"], 21: ["retries"],
    22: ["basic"], 23: ["strings"], 24: ["notes"], 25: ["update", "omitted_body_files"],
    26: ["immutable"], 27: ["errors"], 28: ["basic"], 29: ["public_interface"],
    30: ["native_tests"], 31: ["public_interface", "basic"],
}


def collect(repo):
    with tempfile.TemporaryDirectory(prefix="public-behavior-") as work:
        engine = PublicBehavior(repo, work)
        try:
            results = engine.execute()
        finally:
            engine.fixture.close()
    rows = []
    for kind, mapping in (("P2P", P2P), ("F2P", F2P), ("CONTRACT", CONTRACT)):
        for number, names in mapping.items():
            checks = {n: results[n] for n in names}
            statuses = [r["status"] for r in checks.values()]
            status = next((s for s in ("INFRA_ERROR", "INCONCLUSIVE", "FAIL") if s in statuses), "PASS")
            payload = {"result": status, "checks": checks}
            if kind == "F2P" and number == 60 and status == "PASS":
                payload.update({k: v for k, v in results["native_tests"].items() if k not in {"status", "detail"}})
            rows.append({"id": "%s-%03d" % (kind, number), "kind": kind, "status": status,
                         "value": {"type": "json", "value": payload}})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.environ.get("REPO_ROOT", "/workspace/repo"))
    parser.add_argument("--batch", default="")
    parser.add_argument("--observation", default="")
    parser.add_argument("--observations", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    rows = collect(args.repo)
    if args.batch:
        inventory = json.loads((ASSETS / "shared/integration/observations.json").read_text())["observations"]
        ids = {r["id"] for r in inventory if r["batch_id"] == args.batch}
        rows = [r for r in rows if r["id"] in ids]
    if args.observation:
        rows = [r for r in rows if r["id"] == args.observation]
    if args.observations:
        ids = set(args.observations.split(","))
        rows = [r for r in rows if r["id"] in ids]
    if args.out:
        target = Path(args.out); target.parent.mkdir(parents=True, exist_ok=True)
        observations = {r["id"]: {"status": r["status"], "result": r["status"], "detail": r["value"]} for r in rows}
        if args.batch == "BATCH-F2P-0001":
            observations = [{"observation_id": r["id"], "status": r["status"], "detail": r["value"]} for r in rows]
        target.write_text(json.dumps({"observations": observations}, indent=2) + "\n")
    for row in rows:
        print(json.dumps({"observation_id": row["id"], "status": row["status"], "result": row["status"], "value": row["value"]}))
    return 0 if rows and all(r["status"] == "PASS" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

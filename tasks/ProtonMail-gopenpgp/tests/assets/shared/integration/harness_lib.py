#!/usr/bin/env python3
"""Shared runtime harness for BATCH-INTEGRATION-0001.

The harness dispatches every observation owned by the migrated MIME workstream
(P2P, F2P and DIFFERENTIAL) against the repository under test and emits a
migration-observations:v6 report.

It is side aware only in the sense that it is invoked from the source and the
target runtime entry points; every observation is derived from a real probe
execution, never from a hardcoded side expectation.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from go_source import GoSource, module_requirements

# --------------------------------------------------------------------------
# observation inventory
# --------------------------------------------------------------------------

REQUIRED = [
    "P2P-001", "P2P-002", "P2P-003", "P2P-004", "P2P-005", "P2P-006",
    "P2P-007", "P2P-008", "P2P-009", "P2P-010", "P2P-011", "P2P-012",
    "P2P-013", "P2P-014", "P2P-015", "P2P-016", "P2P-017", "P2P-018",
    "P2P-019",
    "F2P-001", "F2P-002", "F2P-003", "F2P-004", "F2P-005", "F2P-006",
    "F2P-007", "F2P-008", "F2P-009", "F2P-010", "F2P-011", "F2P-012",
    "F2P-013", "F2P-014", "F2P-015", "F2P-016", "F2P-017", "F2P-018",
    "F2P-019", "F2P-020", "F2P-021", "F2P-022", "F2P-023", "F2P-024",
    "F2P-025", "F2P-026", "F2P-027", "F2P-028", "F2P-029", "F2P-030",
    "F2P-031", "F2P-032", "F2P-033", "F2P-034", "F2P-035", "F2P-036",
    "F2P-037", "F2P-038", "F2P-039", "F2P-040", "F2P-041", "F2P-042",
    "F2P-043", "F2P-044", "F2P-045", "F2P-046", "F2P-047", "F2P-048",
    "F2P-049", "F2P-050", "F2P-051", "F2P-052", "F2P-053", "F2P-054",
    "F2P-055", "F2P-056", "F2P-057", "F2P-058", "F2P-059", "F2P-060",
    "F2P-061", "F2P-062", "F2P-063", "F2P-064", "F2P-065", "F2P-066",
    "F2P-067", "F2P-068", "F2P-069", "F2P-070", "F2P-071", "F2P-072",
    "DIFF-001", "DIFF-002", "DIFF-003", "DIFF-004", "DIFF-005", "DIFF-006",
    "DIFF-007", "DIFF-008", "DIFF-009", "DIFF-010", "DIFF-011", "DIFF-012",
    "DIFF-013", "DIFF-014", "DIFF-015", "DIFF-016", "DIFF-017", "DIFF-018",
    "DIFF-019",
]

KIND = {}
for _i in REQUIRED:
    KIND[_i] = "P2P" if _i.startswith("P2P") else (
        "F2P" if _i.startswith("F2P") else "DIFFERENTIAL")

# --------------------------------------------------------------------------
# probe maps
# --------------------------------------------------------------------------

P2P_TESTS = {
    "TestP2P002DecryptionFailure": ["P2P-002"],
    "TestP2P003AndP2P004ExportedAPI": ["P2P-003", "P2P-004"],
    "TestP2P007OnBodyInvalidUTF8": ["P2P-007"],
    "TestP2P008UnreadableMIME": ["P2P-008"],
    "TestP2P010MultipartSignedVerification": ["P2P-010"],
    "TestP2P011CallbackOrder": ["P2P-011"],
    "TestP2P012VerifyHandleNilBehavior": ["P2P-012"],
    "TestP2P013CallbacksInterface": ["P2P-013"],
    "TestP2P014EncryptedHeaders": ["P2P-014"],
    "TestP2P015DualSignatureFailure": ["P2P-015"],
    "TestP2P016OnVerifiedStatuses": ["P2P-016"],
    "TestP2P018TypedSignatureErrors": ["P2P-018"],
    "TestP2P019Attachments": ["P2P-019"],
}
P2P_STRUCTURAL = ["P2P-001", "P2P-005", "P2P-017"]

F2P1_IDS = ["F2P-%03d" % n for n in range(1, 25)]

F2P2_TESTS = {
    "TestProbeGoMessageCharsetDecoding": [
        "F2P-025", "F2P-027", "F2P-033", "F2P-043", "F2P-045"],
    "TestProbeUnknownCharsetTolerated": [
        "F2P-037", "F2P-038", "F2P-042", "F2P-046"],
    "TestProbeUnknownEncodingTolerated": ["F2P-040", "F2P-031"],
    "TestProbeBodySelection": ["F2P-026", "F2P-032", "F2P-036", "F2P-048"],
    "TestProbeContentDispositionAndHeaders": [
        "F2P-028", "F2P-030", "F2P-035", "F2P-041"],
    "TestProbeRawSignedPart": ["F2P-029", "F2P-034"],
}
F2P2_STRUCTURAL = ["F2P-039", "F2P-044", "F2P-047"]

F2P3_TESTS = {
    "TestProbeGoMessageMultipartSignedRaw": ["F2P-072"],
    "TestProbeGoMessageAttachmentDisposition": [
        "F2P-049", "F2P-059", "F2P-062", "F2P-067"],
    "TestProbeGoMessageUnknownCharset": [
        "F2P-050", "F2P-052", "F2P-053", "F2P-054", "F2P-071"],
    "TestProbeGoMessageCharset": ["F2P-051"],
    "TestProbeGoMessageAttachmentHeaderBlock": ["F2P-055", "F2P-064"],
    "TestProbeGoMessageUnknownEncoding": [
        "F2P-056", "F2P-060", "F2P-063", "F2P-066", "F2P-069", "F2P-070"],
    "TestProbeGoMessageMixedTraversal": ["F2P-057", "F2P-065"],
    "TestProbeGoMessageAlternativeSelection": [
        "F2P-058", "F2P-061", "F2P-068"],
}

DIFF_IDS = ["DIFF-%03d" % n for n in range(1, 20)]

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

TARGET_MODULE = "github.com/emersion/go-message"
SOURCE_MODULE = "github.com/ProtonMail/go-mime"
TARGET_MODULE_VERSION = "v0.18.0"

ALLOWED_CHANGED_FILES = {
    "mime/mime.go",
    "mime/signature_collector.go",
    "mime/mime_test.go",
    "go.mod",
    "go.sum",
}


def _sh(cmd, cwd, env, timeout=900):
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              timeout=timeout)
        return proc.returncode, proc.stdout
    except subprocess.TimeoutExpired:
        return 124, "timeout after %ss: %s" % (timeout, " ".join(cmd))
    except OSError as exc:
        return 127, "cannot execute %s: %s" % (" ".join(cmd), exc)


def _repo_root(side):
    for key in ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO"):
        value = os.environ.get(key)
        if value:
            return value
    return os.environ.get("MIGRATION_REPO_ROOT") or "/workspace/repo"


def _go_env(repo, work):
    env = os.environ.copy()
    repo_mod = os.path.join(repo, ".gomodcache")
    if os.path.isdir(repo_mod):
        env["GOMODCACHE"] = repo_mod
    elif not os.path.isdir(env.get("GOMODCACHE", "")) and os.path.isdir(
            "/opt/migration/dependencies/gomodcache"):
        env["GOMODCACHE"] = "/opt/migration/dependencies/gomodcache"
    env.setdefault("GOCACHE", os.path.join(work, "gocache"))
    env["GOFLAGS"] = env.get("GOFLAGS") or "-mod=mod"
    env["GOPROXY"] = env.get("GOPROXY") or "off"
    env["GOSUMDB"] = env.get("GOSUMDB") or "off"
    env.setdefault("GONOSUMDB", "*")
    return env


def _stage(repo, work):
    dst = tempfile.mkdtemp(prefix="stage-", dir=work)
    ignore = shutil.ignore_patterns(".git", ".gocache", ".gomodcache")
    shutil.copytree(repo, dst, ignore=ignore, symlinks=True,
                    dirs_exist_ok=True)
    return dst


def _parse_go_test_json(output):
    """Return {test_name: 'pass'|'fail'|'skip'} from `go test -json` output."""
    results = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        action = event.get("Action")
        test = event.get("Test")
        if test and action in ("pass", "fail", "skip"):
            results[test] = action
    return results


def _hashed_tree(root):
    hashes = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            try:
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                continue
            hashes[os.path.relpath(path, root).replace(os.sep, "/")] = digest
    return hashes


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


# --------------------------------------------------------------------------
# structural checks
# --------------------------------------------------------------------------

def check_patch_collectable(repo):
    if not os.path.isdir(os.path.join(repo, ".git")):
        if os.path.isfile(os.path.join(repo, "go.mod")):
            return True, "working tree present (no git metadata)"
        return False, "no working tree and no git metadata"
    env = os.environ.copy()
    rc, _ = _sh(["git", "diff", "--binary", "--exit-code"], repo, env)
    if rc in (0, 1):
        return True, "git working tree diff collectable"
    rc2, out2 = _sh(["git", "status", "--porcelain"], repo, env)
    if rc2 == 0:
        return True, "git working tree collectable"
    return False, out2.strip()[:400]


def check_testdata_unchanged(repo, asset_root):
    expected_path = os.path.join(asset_root, "fixtures", "integration",
                                 "expected_testdata_hashes.json")
    expected = json.loads(_read(expected_path) or "{}")
    actual = _hashed_tree(os.path.join(repo, "mime", "testdata"))
    changed = []
    for name, digest in expected.items():
        if actual.get(name) != digest:
            changed.append(name)
    if changed:
        return False, "mime/testdata changed: %s" % ",".join(sorted(changed))
    return True, "%d testdata fixtures byte-identical" % len(expected)


def check_changed_files(repo):
    if not os.path.isdir(os.path.join(repo, ".git")):
        return True, "no git metadata; changed file set not observable"
    env = os.environ.copy()
    rc, out = _sh(["git", "diff", "--name-only", "HEAD"], repo, env)
    if rc != 0:
        return True, "changed file set not observable"
    changed = [line.strip() for line in out.splitlines() if line.strip()]
    outside = [name for name in changed if name not in ALLOWED_CHANGED_FILES]
    if outside:
        return False, "changes outside the allowed set: %s" % ",".join(outside)
    return True, "changed files within allowed set: %s" % ",".join(sorted(changed))


def _mime_production_sources(repo):
    """All production ``.go`` files of the ``mime`` package (tests excluded).

    A hidden source-runtime fallback may live in any file of the package, not
    only the two originally named ones, so the anti-bridge scan must cover the
    whole production surface (RG-C).
    """

    mime_dir = os.path.join(repo, "mime")
    if not os.path.isdir(mime_dir):
        return []
    return [
        name
        for name in sorted(os.listdir(mime_dir))
        if name.endswith(".go") and not name.endswith("_test.go")
    ]


def check_go_mod_target(repo):
    mod = _read(os.path.join(repo, "go.mod"))
    requirements = module_requirements(mod)
    ok_target = requirements.get(TARGET_MODULE) == TARGET_MODULE_VERSION
    ok_source = SOURCE_MODULE in requirements
    if ok_target and not ok_source:
        return True, "go.mod requires %s %s and not %s" % (
            TARGET_MODULE, TARGET_MODULE_VERSION, SOURCE_MODULE)
    return False, "go.mod target=%s source=%s" % (ok_target, ok_source)


def check_mime_imports_target(repo):
    imports = _mime_import_paths(repo)
    uses_target = any(p == TARGET_MODULE or p.startswith(TARGET_MODULE + "/") for p in imports)
    uses_source = any(p == SOURCE_MODULE or p.startswith(SOURCE_MODULE + "/") for p in imports)
    if uses_target and not uses_source:
        return True, "mime package production sources use %s and not %s" % (
            TARGET_MODULE, SOURCE_MODULE)
    return False, "mime package production sources target=%s source=%s" % (
        uses_target, uses_source)


def _mime_import_paths(repo):
    return {package for name in _mime_production_sources(repo)
            for _, package in GoSource(_read(os.path.join(repo, "mime", name))).imports}


def check_runtime_dependency(repo):
    imports = _mime_import_paths(repo)
    forbidden = ["os/exec", "net/rpc", "plugin", "syscall", "unsafe",
                 "C"]
    hits = [token for token in forbidden if token in imports]
    if any(p == SOURCE_MODULE or p.startswith(SOURCE_MODULE + "/") for p in imports):
        return False, "mime production code still imports %s" % SOURCE_MODULE
    if hits:
        return False, "mime production code uses %s" % ",".join(hits)
    if not any(p == TARGET_MODULE or p.startswith(TARGET_MODULE + "/") for p in imports):
        return False, "mime production code does not import %s" % TARGET_MODULE
    return True, "no subprocess/RPC/FFI delegation of go-mime responsibilities"


# --------------------------------------------------------------------------
# probe execution
# --------------------------------------------------------------------------

def _build_failure_is_target_absent(output):
    markers = [
        "cannot find module providing package github.com/emersion/go-message",
        "no required module provides package github.com/emersion/go-message",
        "module lookup disabled by GOPROXY=off",
        "cannot find module providing package github.com/emersion/go-message/",
    ]
    return any(marker in output for marker in markers)


def run_go_probe(repo, work, probe_sources, run_args, go_env, asset_root):
    """Copy probe files into a staged repository and run `go test -json`.

    probe_sources: list of (source_path, destination_relative_path).
    run_args: additional `go test` arguments (e.g. ["-run", "^TestProbe"]).
    Returns (per_test_results, raw_output, build_failed).
    """
    staged = _stage(repo, work)
    for source_path, dest_rel in probe_sources:
        dest = os.path.join(staged, dest_rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(asset_root, source_path), dest)
    cmd = ["go", "test", "-json", "-count=1"] + run_args + ["./mime/"]
    rc, out = _sh(cmd, staged, go_env)
    results = _parse_go_test_json(out)
    return results, out, rc


def run_go_program(repo, work, source_path, dest_pkg, go_env, asset_root):
    staged = _stage(repo, work)
    dest_dir = os.path.join(staged, dest_pkg)
    os.makedirs(dest_dir, exist_ok=True)
    shutil.copyfile(os.path.join(asset_root, source_path),
                    os.path.join(dest_dir, "main.go"))
    rc, out = _sh(["go", "run", "./" + dest_pkg], staged, go_env)
    return rc, out


# --------------------------------------------------------------------------
# per-batch orchestration
# --------------------------------------------------------------------------

def _record(obs, oid, status, value, detail=None):
    # P2P/DIFFERENTIAL values must be a stable projection that normalizes to the
    # same value on both sides, so only F2P carries the free-form probe detail.
    if detail is not None and oid.startswith("F2P") and isinstance(value, dict):
        value = dict(value)
        value["detail"] = detail
    obs[oid] = {
        "id": oid,
        "kind": KIND[oid],
        "status": status,
        "value": {"type": "json", "value": value},
    }


def collect_p2p(repo, work, go_env, asset_root, obs):
    sources = [
        ("target/batches/BATCH-P2P-0001/p2p_probe_test.go",
         "mime/zz_p2p_probe_test.go"),
    ]
    results, out, _ = run_go_probe(
        repo, work, sources, ["-run", "^TestP2P"], go_env, asset_root)
    build_bad = _build_failure_is_target_absent(out)
    for test, ids in P2P_TESTS.items():
        state = results.get(test)
        for oid in ids:
            if state == "pass":
                _record(obs, oid, "PASS", {"result": "PASS"},
                        "probe %s passed" % test)
            elif state == "fail":
                _record(obs, oid, "FAIL", {"result": "FAIL"},
                        "probe %s failed" % test)
            elif build_bad:
                _record(obs, oid, "FAIL", {"result": "FAIL"},
                        "target technology absent during probe build")
            else:
                _record(obs, oid, "INCONCLUSIVE", {"result": "INCONCLUSIVE"},
                        "probe %s did not report" % test)

    # structural observations
    ok, detail = check_patch_collectable(repo)
    _record(obs, "P2P-001", "PASS" if ok else "FAIL",
            {"result": "PASS" if ok else "FAIL"}, detail)
    ok, detail = check_testdata_unchanged(repo, asset_root)
    _record(obs, "P2P-005", "PASS" if ok else "FAIL",
            {"result": "PASS" if ok else "FAIL"}, detail)
    ok, detail = check_changed_files(repo)
    _record(obs, "P2P-017", "PASS" if ok else "FAIL",
            {"result": "PASS" if ok else "FAIL"}, detail)

    # P2P-006: out-of-scope packages keep their public behavior.
    rc, out = _sh(["go", "test", "-count=1", "./crypto/", "./armor/",
                   "./constants/", "./profile/", "./mobile/"],
                  _stage_simple(repo, work, "p2p006"), go_env)
    ok = rc == 0
    _record(obs, "P2P-006", "PASS" if ok else "FAIL",
            {"result": "PASS" if ok else "FAIL"},
            "go test of out-of-scope packages rc=%s" % rc)

    # P2P-009: preserved mime/mime_test.go behavioral assertions.
    rc, out = _sh(["go", "test", "-count=1", "-run", "TestMessageVerification",
                   "./mime/"], _stage_simple(repo, work, "p2p009"), go_env)
    ok = rc == 0
    _record(obs, "P2P-009", "PASS" if ok else "FAIL",
            {"result": "PASS" if ok else "FAIL"},
            "go test -run TestMessageVerification ./mime rc=%s" % rc)


def _stage_simple(repo, work, tag):
    dest = os.path.join(work, "stage-" + tag)
    if not os.path.isdir(dest):
        ignore = shutil.ignore_patterns(".git", ".gocache", ".gomodcache")
        shutil.copytree(repo, dest, ignore=ignore, symlinks=True)
    return dest


def collect_f2p0001(repo, work, go_env, asset_root, obs):
    rc, out = run_go_program(
        repo, work, "target/batches/BATCH-F2P-0001/probe/main.go",
        "f2pprobe", go_env, asset_root)
    parsed = None
    if rc == 0:
        start = out.find("[")
        end = out.rfind("]")
        if start != -1 and end != -1:
            try:
                parsed = json.loads(out[start:end + 1])
            except ValueError:
                parsed = None
    got = {}
    if parsed:
        for item in parsed:
            got[item.get("observation_id")] = item
    for oid in F2P1_IDS:
        item = got.get(oid)
        if item is None:
            if _build_failure_is_target_absent(out):
                _record(obs, oid, "FAIL", {"result": "FAIL"},
                        "target technology absent")
            else:
                _record(obs, oid, "INCONCLUSIVE", {"result": "INCONCLUSIVE"},
                        "probe did not report %s" % oid)
            continue
        if item.get("pass"):
            _record(obs, oid, "PASS", {"result": "PASS", "detail": item.get("detail", "")},
                    item.get("detail", ""))
        else:
            _record(obs, oid, "FAIL", {"result": "FAIL", "detail": item.get("detail", "")},
                    item.get("detail", ""))


def collect_f2p0002(repo, work, go_env, asset_root, obs):
    sources = [
        ("target/batches/BATCH-F2P-0002/probe_target_test.go",
         "mime/zz_f2p2_probe_test.go"),
    ]
    results, out, _ = run_go_probe(
        repo, work, sources, ["-run", "^TestProbe"], go_env, asset_root)
    build_bad = _build_failure_is_target_absent(out)
    for test, ids in F2P2_TESTS.items():
        state = results.get(test)
        for oid in ids:
            if state == "pass":
                _record(obs, oid, "PASS", {"result": "PASS"},
                        "probe %s passed" % test)
            elif state == "fail":
                _record(obs, oid, "FAIL", {"result": "FAIL"},
                        "probe %s failed" % test)
            elif build_bad:
                _record(obs, oid, "FAIL", {"result": "FAIL"},
                        "target technology absent during probe build")
            else:
                _record(obs, oid, "INCONCLUSIVE", {"result": "INCONCLUSIVE"},
                        "probe %s did not report" % test)
    _structural_f2p2(repo, obs)


def _structural_f2p2(repo, obs):
    ok, detail = check_go_mod_target(repo)
    _record(obs, "F2P-039", "PASS" if ok else "FAIL",
            {"result": "PASS" if ok else "FAIL"}, detail)
    ok, detail = check_runtime_dependency(repo)
    _record(obs, "F2P-044", "PASS" if ok else "FAIL",
            {"result": "PASS" if ok else "FAIL"}, detail)
    ok, detail = check_mime_imports_target(repo)
    _record(obs, "F2P-047", "PASS" if ok else "FAIL",
            {"result": "PASS" if ok else "FAIL"}, detail)


def collect_f2p0003(repo, work, go_env, asset_root, obs):
    target_available, detail = check_go_mod_target(repo)
    if not target_available:
        for ids in F2P3_TESTS.values():
            for oid in ids:
                _record(obs, oid, "FAIL", {"result": "FAIL"}, detail)
        return
    sources = [
        ("target/batches/BATCH-F2P-0002/probe_target_test.go",
         "mime/zz_f2p2_probe_test.go"),
        ("target/batches/BATCH-F2P-0003/probe_test.go",
         "mime/zz_f2p3_probe_test.go"),
    ]
    selected = "^(" + "|".join(re.escape(name) for name in F2P3_TESTS) + ")$"
    results, out, _ = run_go_probe(
        repo, work, sources, ["-run", selected], go_env, asset_root)
    build_bad = _build_failure_is_target_absent(out)
    for test, ids in F2P3_TESTS.items():
        state = results.get(test)
        for oid in ids:
            if state == "pass":
                _record(obs, oid, "PASS", {"result": "PASS"},
                        "probe %s passed" % test)
            elif state == "fail":
                _record(obs, oid, "FAIL", {"result": "FAIL"},
                        "probe %s failed" % test)
            elif build_bad:
                _record(obs, oid, "FAIL", {"result": "FAIL"},
                        "target technology absent during probe build")
            else:
                _record(obs, oid, "INCONCLUSIVE", {"result": "INCONCLUSIVE"},
                        "probe %s did not report" % test)


def collect_diff(repo, work, go_env, asset_root, obs):
    rc, out = run_go_program(
        repo, work,
        "shared/batches/BATCH-DIFFERENTIAL-0001/diffprobe/main.go",
        "diffprobe", go_env, asset_root)
    report = None
    if rc == 0:
        start = out.find("{")
        end = out.rfind("}")
        if start != -1 and end != -1:
            try:
                report = json.loads(out[start:end + 1])
            except ValueError:
                report = None

    if report is None:
        for oid in DIFF_IDS:
            status = "FAIL" if _build_failure_is_target_absent(out) else "INCONCLUSIVE"
            _record(obs, oid, status, {"result": status},
                    "differential probe did not produce a report")
        return

    scenarios = report.get("scenarios", [])
    by_name = {s.get("name", ""): s for s in scenarios}

    def scenario_projection(field):
        return {name: s.get(field) for name, s in sorted(by_name.items())}

    diff_values = {
        "DIFF-001": report.get("decrypt_signature"),
        "DIFF-002": report.get("callback_methods"),
        "DIFF-003": scenario_projection("on_encrypted_headers"),
        "DIFF-004": scenario_projection("on_verified"),
        "DIFF-005": scenario_projection("on_error"),
        "DIFF-006": scenario_projection("on_error"),
        "DIFF-007": scenario_projection("on_error"),
        "DIFF-008": scenario_projection("on_verified"),
        "DIFF-009": scenario_projection("on_error"),
        "DIFF-010": scenario_projection("on_verified"),
        "DIFF-011": scenario_projection("on_body"),
        "DIFF-012": scenario_projection("on_attachment"),
        "DIFF-013": scenario_projection("on_encrypted_headers"),
        "DIFF-014": {
            "decrypt_signature": report.get("decrypt_signature"),
            "callback_methods": report.get("callback_methods"),
        },
        "DIFF-015": None,
        "DIFF-016": {
            "go_crypto_present": report.get("go_crypto_present"),
            "go_mod_requires_normalized": report.get("go_mod_requires_normalized"),
        },
        "DIFF-017": report.get("testdata_hashes"),
        "DIFF-018": None,
        "DIFF-019": None,
    }

    ok, detail = check_changed_files(repo)
    diff_values["DIFF-015"] = detail if not ok else []
    ok, detail = check_patch_collectable(repo)
    diff_values["DIFF-019"] = "PRESENT" if ok else "ABSENT"

    rc, _out = _sh(["go", "test", "-count=1", "-run",
                    "TestMessageVerification", "./mime/"],
                   _stage_simple(repo, work, "diff018"), go_env)
    diff_values["DIFF-018"] = "PASS" if rc == 0 else "FAIL"

    for oid in DIFF_IDS:
        value = diff_values.get(oid)
        status = "PASS"
        if oid == "DIFF-012" and (
            not isinstance(value, dict)
            or not value
            or all(item is None for item in value.values())
        ):
            # The differential corpus has no attachment scenarios, so the
            # on_attachment projection is empty.  Record NA rather than a
            # vacuous PASS so an attachment regression cannot hide (RG-H).
            status = "NA"
        _record(obs, oid, status, value, "normalized differential projection")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def _load_required(asset_root):
    path = os.path.join(asset_root, "shared", "integration",
                        "observations.json")
    try:
        data = json.loads(_read(path))
        ids = [item["id"] for item in data["observations"]]
        if ids:
            return ids
    except (ValueError, KeyError, TypeError):
        pass
    return list(REQUIRED)


def main(side, report_path, asset_root):
    repo = _repo_root(side)
    work = tempfile.mkdtemp(prefix="mig-integration-")
    go_env = _go_env(repo, work)
    obs = {}
    collectors = [
        collect_p2p,
        collect_f2p0001,
        collect_f2p0002,
        collect_f2p0003,
        collect_diff,
    ]
    try:
        for collector in collectors:
            try:
                collector(repo, work, go_env, asset_root, obs)
            except Exception as exc:  # noqa: BLE001 - keep the report well formed
                sys.stderr.write("integration harness: %s failed: %s\n" % (
                    collector.__name__, exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    ordered = []
    for oid in _load_required(asset_root):
        if oid in obs:
            ordered.append(obs[oid])
        else:
            ordered.append({
                "id": oid,
                "kind": KIND.get(oid, "P2P"),
                "status": "INCONCLUSIVE",
                "value": {"type": "json", "value": {"result": "INCONCLUSIVE"}},
                "detail": "observation not produced",
            })

    report = {
        "schema_version": "migration-observations:v6",
        "batch_id": "BATCH-INTEGRATION-0001",
        "observations": ordered,
    }
    payload = json.dumps(report, indent=2, sort_keys=False)
    if report_path and report_path not in ("-", ""):
        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    else:
        sys.stdout.write(payload + "\n")
    return 0

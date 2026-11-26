#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
from pathlib import Path


BATCH_ID = "BATCH-P2P-0001"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "shared", "integration"))
from serie_bind import module_root

SHARED = os.path.abspath(
    os.path.join(HERE, "..", "..", "..", "shared", "batches", BATCH_ID, "observations.json")
)
PROTECTED_PATHS = (
    "src/app.rs",
    "src/view",
    "src/widget",
    "src/protocol.rs",
    "src/graph/image.rs",
    "src/graph/geometry.rs",
    "src/color.rs",
    "src/keybind.rs",
    "src/config.rs",
    "src/external.rs",
    "src/main.rs",
)


_CACHE_PARTS = (".git", ".gocache", ".gomodcache", "vendor")


def _has_go_sources(directory):
    for root, dirs, files in os.walk(directory):
        dirs[:] = [name for name in dirs if name not in _CACHE_PARTS]
        if any(name.endswith(".go") for name in files):
            return True
    return False


def _allowed_go_change(repo, path):
    """True when a working-tree path belongs to the candidate's Go core/module.

    The task only constrains changes to "the new Go core, its Go module and
    build files, and supporting Go tests or fixtures"; it never fixes the Go
    package directory, so any Go source tree, module file, or document placed
    beside Go sources is in scope.
    """

    clean = path.strip().strip('"')
    if not clean:
        return False
    if clean.startswith((".gocache/", ".gomodcache/")):
        return True
    stripped = clean.rstrip("/")
    if stripped in ("go.mod", "go.sum") or stripped.endswith(".go"):
        return True
    # Root-level Go documentation/build wiring is allowed when a Go core is
    # present. Protected Rust paths still have independent content checks.
    name = stripped.rsplit("/", 1)[-1]
    if name in ("Makefile", "GNUmakefile") or name.lower().endswith(".md"):
        parent = Path(repo) / os.path.dirname(stripped)
        if _has_go_sources(parent):
            return True
    if stripped.split("/", 1)[0] in ("cmd", "core"):
        return True
    if clean.endswith("/"):
        return _has_go_sources(os.path.join(repo, stripped))
    directory = os.path.dirname(stripped)
    if not directory:
        return False
    return _has_go_sources(os.path.join(repo, directory))


def _has_candidate_go_tests(repo):
    for root, dirs, files in os.walk(repo):
        dirs[:] = [name for name in dirs if name not in _CACHE_PARTS]
        relative = os.path.relpath(root, repo)
        if relative in ("src/tests", "tests/graph"):
            continue
        if any(name.endswith("_test.go") for name in files):
            return True
    return False


def run(command, cwd, env=None):
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def git_status(repo, *paths):
    result = run(["git", "status", "--porcelain", "--", *paths], repo)
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def index_independent_changes(repo, *paths):
    """Detect working-tree changes without trusting the Git index.

    ``git status``/``git diff`` honour ``--assume-unchanged`` and
    ``skip-worktree``; a candidate process can set those at runtime (for
    example from a Go ``init()``) to hide a weakened protected test.  Compare
    each tracked file's hashed working-tree content against the committed base
    object directly, which the index cannot forge.
    """
    listed = run(["git", "ls-files", "-z", "--", *paths], repo)
    if listed.returncode != 0:
        return None
    changes = []
    for rel in (entry for entry in listed.stdout.split("\0") if entry):
        head = run(["git", "rev-parse", "--verify", "HEAD:" + rel], repo)
        if head.returncode != 0:
            changes.append(rel + " (not in base revision)")
            continue
        full = os.path.join(repo, rel)
        if not os.path.isfile(full) or os.path.islink(full):
            changes.append(rel + " (deleted)")
            continue
        obj = run(["git", "hash-object", "--path=" + rel, "--", rel], repo)
        if obj.returncode != 0 or obj.stdout.strip() != head.stdout.strip():
            changes.append(rel + " (content changed)")
    others = run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *paths], repo
    )
    if others.returncode == 0:
        for rel in (entry for entry in others.stdout.split("\0") if entry):
            changes.append(rel + " (added)")
    return changes


def worktree_changes(repo, *paths):
    """Return status lines plus index-independent content changes."""

    lines = git_status(repo, *paths)
    if lines is None:
        return None
    hidden = index_independent_changes(repo, *paths)
    if hidden is None:
        return None
    for item in hidden:
        if item not in lines:
            lines.append(item)
    return lines


def parse_go_test_json(output):
    statuses = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        test = event.get("Test")
        action = event.get("Action")
        if test and action in ("pass", "fail", "skip"):
            statuses[test] = action
    return statuses


def structural_check(name, repo, statuses):
    if name == "target-change-scope":
        lines = git_status(repo)
        if lines is None:
            return False, "target is not a git repository"
        hidden = index_independent_changes(repo, "src/tests", "tests/graph", *PROTECTED_PATHS)
        if hidden is None:
            return False, "target is not a git repository"
        if hidden:
            return False, "hidden change to protected tests: " + "; ".join(hidden)
        for line in lines:
            code = line[:2]
            path = line[3:].strip()
            if code == "??":
                if _allowed_go_change(repo, path):
                    continue
                return False, "unexpected added path: " + path
            if code.strip() in ("M", "A"):
                if _allowed_go_change(repo, path):
                    continue
            return False, "unexpected tracked change: " + line
        return True, "diff contains only the candidate Go core, Go module, and supporting Go test files"

    if name == "target-protected-paths":
        lines = worktree_changes(repo, *PROTECTED_PATHS)
        if lines is None:
            return False, "target is not a git repository"
        if lines:
            return False, "protected paths changed: " + "; ".join(lines)
        return True, "out-of-scope Rust paths are unchanged"

    if name == "target-test-wiring":
        lines = worktree_changes(repo, "src/tests", "tests/graph")
        if lines is None:
            return False, "target is not a git repository"
        if lines:
            return False, "protected Rust tests or snapshots changed: " + "; ".join(lines)
        if not _has_candidate_go_tests(repo):
            return False, "missing Go test wiring: no candidate Go _test.go files were found"
        return True, "protected Rust assertions remain and Go test wiring is present"

    if name == "target-protected-tests":
        lines = worktree_changes(repo, "src/tests", "tests/graph")
        if lines is None:
            return False, "target is not a git repository"
        if lines:
            return False, "protected Rust tests or PNG snapshots changed: " + "; ".join(lines)
        return True, "src/tests and tests/graph are unchanged"

    if name == "target-schema-compat":
        result = run(["git", "diff", "--exit-code", "--", "config.schema.json"], repo)
        if result.returncode != 0:
            return False, "config.schema.json changed"
        return True, "config.schema.json and the configuration file format are unchanged"

    if name == "target-rust-baseline":
        lines = worktree_changes(repo, "src")
        if lines is None:
            return False, "target is not a git repository"
        modified = [line for line in lines if not line.startswith("??")]
        if modified:
            return False, "Rust baseline files changed: " + "; ".join(modified)
        missing = [path for path in PROTECTED_PATHS if not os.path.exists(os.path.join(repo, path))]
        if missing:
            return False, "protected Rust modules are missing: " + ", ".join(missing)
        return True, "out-of-scope Rust modules are present and unchanged"

    return False, "unknown structural check: " + name


def main():
    repo = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "/workspace/repo"
    report = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.join(HERE, "result.json")
    with open(SHARED, "r", encoding="utf-8") as handle:
        spec = json.load(handle)

    env = dict(os.environ)
    env.setdefault("CGO_ENABLED", "0")
    env.setdefault("GOPROXY", "off")
    go = run(["go", "test", "-json", "./..."], module_root(Path(repo)), env=env)
    statuses = parse_go_test_json(go.stdout)
    go_ok = go.returncode == 0

    observations = {}
    failed = []
    for observation in spec["observations"]:
        probe = observation["target_probe"]
        if probe["kind"] == "go-test":
            tests = probe.get("tests") or []
            if tests:
                bad = [
                    test
                    for test in tests
                    if statuses.get(test) != "pass"
                ]
                ok = not bad
                detail = (
                    "all selected Go tests passed"
                    if ok
                    else "Go tests did not pass: " + ", ".join(bad)
                )
            else:
                ok = go_ok
                detail = "full go test ./... passed" if ok else "go test ./... failed"
        else:
            ok, detail = structural_check(probe["check"], repo, statuses)

        value = {"result": "PASS"} if ok else {"result": "FAIL", "detail": detail}
        observations[observation["id"]] = value
        if not ok:
            failed.append(observation["id"])

    payload = {
        "batch_id": BATCH_ID,
        "side": "target",
        "relation": spec["relation"],
        "observations": observations,
    }
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with open(report, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(json.dumps(payload, sort_keys=True))
    if failed:
        print("failed observations: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

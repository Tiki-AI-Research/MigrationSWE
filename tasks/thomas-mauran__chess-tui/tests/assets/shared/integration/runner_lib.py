#!/usr/bin/env python3
"""Shared runtime harness for the INTEGRATION batch.

The source/ and target/ entry points are thin wrappers over this module. It
reads the self-contained observation inventory written during authoring
(``shared/integration/observations.json``) together with the probe map
(``shared/integration/probe-map.json``), executes the referenced probe for
every observation against the repository under test, and emits a
``migration-observations:v6`` runtime report on stdout.

The harness is intentionally self-contained: it never reads ``dependencies/``,
``fragments/`` or any authoring-private path at runtime. Every probe is a real
execution (a Go test overlay, or a shell probe shipped with the batch) rather
than a declared expectation; a probe that cannot run is reported as
INCONCLUSIVE instead of being guessed.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPORT_SCHEMA = "migration-observations:v6"
VALID_STATUSES = ("PASS", "FAIL", "NA", "INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT")

_REPO_ENV_KEYS = ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO")

# Third-party modules imported by the target go-test probes.  They are probe
# dependencies (the task ships them in the offline module cache), not a
# requirement the candidate's ``go.mod`` must declare.
_PROBE_MODULES = (
    ("github.com/BurntSushi/toml", "v1.6.0"),
    ("github.com/notnil/chess", "v1.10.0"),
)

# Per-suite deadline for an overlaid probe ``go test`` run.  The probes are
# fast (contract observations with short internal deadlines); this only caps a
# candidate implementation that blocks, so several blocked suites can never
# exceed the verifier's overall execution budget.
_GO_TEST_TIMEOUT = "90s"


def asset_root():
    """Root of the published asset tree (parent of shared/, source/, target/)."""
    here = os.path.dirname(os.path.abspath(__file__))  # shared/integration
    return os.path.dirname(os.path.dirname(here))


def resolve_repo(explicit):
    if explicit:
        return os.path.abspath(explicit)
    for key in _REPO_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            return os.path.abspath(value)
    return "/workspace/repo"


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def load_inventory(root):
    data = load_json(os.path.join(root, "shared", "integration", "observations.json"))
    if not isinstance(data, dict):
        return []
    return data.get("observations") or []


def load_probe_map(root):
    data = load_json(os.path.join(root, "shared", "integration", "probe-map.json"))
    return data if isinstance(data, dict) else {}


def _go_env(repo=None):
    env = dict(os.environ)
    env.setdefault("GOPROXY", "off")
    env.setdefault("GOSUMDB", "off")
    env.setdefault("GOFLAGS", "")
    env.setdefault("GOTOOLCHAIN", "local")
    env.setdefault("CGO_ENABLED", "0")
    # The task materializes its Go module cache next to the repository under
    # test (``<repo>/.gomodcache``). Prefer it when present so the harness does
    # not depend on an authoring-private cache location.
    if repo:
        local_cache = os.path.join(repo, ".gomodcache")
        if os.path.isdir(local_cache):
            env["GOMODCACHE"] = local_cache
    return env


def _go_group_key(spec):
    return (
        spec.get("base"),
        tuple(spec.get("files") or ()),
        tuple(spec.get("packages") or ("./...",)),
        spec.get("dest"),
    )


class ProbeRunner:
    """Executes probe descriptors against a repository tree."""

    def __init__(self, root, side, repo):
        self.root = root
        self.side = side
        self.repo = repo
        self._repo_copy = None
        self._group_cache = {}
        self._probe_paths = {}
        self._group_tests = {}
        for entry in load_probe_map(root).values():
            spec = entry.get(side) if isinstance(entry, dict) else None
            if not isinstance(spec, dict) or spec.get("kind") != "go_test":
                continue
            names = self._group_tests.setdefault(_go_group_key(spec), set())
            names.update(name for name in spec.get("tests") or () if isinstance(name, str) and name)

    # -- helpers ---------------------------------------------------------
    def _probe_file(self, rel):
        """Resolve a probe file relative to the published asset root."""
        return os.path.join(self.root, rel)

    def _working_copy(self):
        if self._repo_copy is None:
            tmp = tempfile.mkdtemp(prefix="migration-%s-" % self.side)
            dest = os.path.join(tmp, "repo")
            shutil.copytree(
                self.repo,
                dest,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git", ".gomodcache", ".gocache"),
            )
            self._repo_copy = dest
        return self._repo_copy

    def _overlay_for(self, base, files, dest):
        repo = self._working_copy()
        mapping = {}
        for rel in files:
            src = self._probe_file(os.path.join(base, rel))
            if not os.path.isfile(src):
                return None
            vdest = dest or rel
            vpath = os.path.join(repo, vdest)
            os.makedirs(os.path.dirname(vpath), exist_ok=True)
            mapping[vpath] = src
        return mapping

    def _has_go_module(self):
        """True when the tree under test ships a Go module.

        F2P postconditions are predicates about the *target* technology. A tree
        that has no ``go.mod`` does not carry the target technology at all, so
        the postcondition is observably unmet there; the probe reports that as
        a real FAIL rather than guessing.
        """
        return os.path.isfile(os.path.join(self.repo, "go.mod"))

    # -- go test probes --------------------------------------------------
    def _resolve_probe_modules(self, repo, env):
        """Make the probe-owned external modules resolvable from the offline cache.

        The probe overlays import a small, fixed set of third-party modules.  A
        candidate implements the migrated behaviour with its own dependency
        choices, so the probe must not fail to compile merely because the
        candidate's ``go.mod`` does not declare exactly those modules.  Adding
        the probe requirements to the throw-away working copy (never to the
        candidate) keeps the execution boundary honest: the probe still exercises
        the candidate's real implementation while its own imports resolve from
        the module cache the task already materializes.
        """
        gomod = os.path.join(repo, "go.mod")
        if not os.path.isfile(gomod):
            return
        try:
            with open(gomod, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return
        missing = ["%s@%s" % (name, version) for name, version in _PROBE_MODULES if name not in text]
        if not missing:
            return
        subprocess.run(
            ["go", "mod", "edit", *("-require=" + item for item in missing)],
            cwd=repo, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
        subprocess.run(
            ["go", "mod", "download", *[name for name, _ in _PROBE_MODULES]],
            cwd=repo, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
        )

    def _run_go_group(self, spec):
        base = spec.get("base")
        files = spec.get("files") or []
        pkg = spec.get("packages") or ["./..."]
        dest = spec.get("dest")
        key = _go_group_key(spec)
        if key in self._group_cache:
            return self._group_cache[key]

        if not self._has_go_module():
            self._group_cache[key] = None
            return None

        overlay = self._overlay_for(base, files, dest) if base else None
        if overlay is None:
            self._group_cache[key] = None
            return None

        work = tempfile.mkdtemp(prefix="overlay-")
        overlay_path = os.path.join(work, "overlay.json")
        with open(overlay_path, "w", encoding="utf-8") as handle:
            json.dump({"Replace": overlay}, handle)

        repo_copy = self._working_copy()
        env = _go_env(self.repo)
        self._resolve_probe_modules(repo_copy, env)

        # Run only tests declared by the published probe map. Candidate-authored
        # tests are still compiled, but they may expect throw-away files from
        # their author's container that do not exist in this scoring container.
        names = self._group_tests.get(key) or set(spec.get("tests") or ())
        if not names:
            self._group_cache[key] = None
            return None
        run_pattern = "^(" + "|".join(re.escape(name) for name in sorted(names)) + ")$"

        # A candidate whose implementation blocks (for example a ``--pgn`` run
        # that never returns, or a UCI/stream read with no deadline) makes the
        # probe ``go test`` binary hang.  Without an explicit deadline the Go
        # toolchain waits its default ten minutes per suite, so a handful of
        # blocked suites exhaust the verifier's overall time budget and turn a
        # genuine postcondition failure into an unscoreable INFRA_ERROR.  Bound
        # each suite so a hang is reported as a real FAIL within the budget.
        cmd = ["go", "test", "-json", "-count=1", "-run=" + run_pattern,
               "-timeout=" + _GO_TEST_TIMEOUT,
               "-overlay=" + overlay_path] + list(pkg)
        try:
            proc = subprocess.run(
                cmd,
                cwd=repo_copy,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=900,
            )
        except (OSError, subprocess.TimeoutExpired):
            self._group_cache[key] = None
            return None
        results = {}
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            test = event.get("Test")
            action = event.get("Action")
            if test and action in ("pass", "fail", "skip"):
                results[test] = action
        self._group_cache[key] = results
        return results

    def _go_test_status(self, spec):
        tests = spec.get("tests") or []
        if not self._has_go_module():
            return "FAIL", "target Go module absent; postcondition not demonstrable"
        results = self._run_go_group(spec)
        if results is None or not tests:
            return "INCONCLUSIVE", "go probe could not be executed"
        seen = [results.get(t) for t in tests]
        if any(s is None for s in seen):
            return "FAIL", "probe test did not report a result"
        if all(s == "pass" for s in seen):
            return "PASS", "go probe suite passed"
        if any(s == "skip" for s in seen):
            return "INCONCLUSIVE", "probe test skipped"
        return "FAIL", "go probe suite failed"

    # -- shell probes ----------------------------------------------------
    def _shell_status(self, spec):
        script = self._probe_file(spec.get("script", ""))
        if not os.path.isfile(script):
            return "INCONCLUSIVE", "shell probe not found"
        try:
            proc = subprocess.run(
                ["sh", script, self.repo],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "INFRA_ERROR", "shell probe could not be executed"
        stdout = proc.stdout.decode("utf-8", "replace")
        result = None
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line).get("result")
                except ValueError:
                    pass
        if result in ("PASS", "FAIL"):
            return result, "shell probe reported %s" % result
        return ("PASS" if proc.returncode == 0 else "FAIL"), "shell probe exit=%d" % proc.returncode

    # -- dispatch --------------------------------------------------------
    def run(self, spec):
        if not isinstance(spec, dict):
            return "INCONCLUSIVE", "no executable probe registered"
        kind = spec.get("kind")
        if kind == "go_test":
            return self._go_test_status(spec)
        if kind == "shell":
            return self._shell_status(spec)
        return "INCONCLUSIVE", "unknown probe kind: %r" % (kind,)


def normalize_value(status, detail):
    """Stable, host-independent projection used for source/target comparison.

    The projection carries only the normalized behavioural result; probe
    diagnostics (paths, durations, free text) are deliberately excluded so the
    source and target values remain comparable after normalization.
    """
    return {"result": status}


def build_report(root, side, repo):
    inventory = load_inventory(root)
    probe_map = load_probe_map(root)
    runner = ProbeRunner(root, side, repo)
    observations = []
    for entry in inventory:
        oid = entry.get("id")
        kind = entry.get("kind")
        spec = (probe_map.get(oid) or {}).get(side)
        status, detail = runner.run(spec)
        if status not in VALID_STATUSES:
            status = "INCONCLUSIVE"
        observations.append({
            "id": oid,
            "kind": kind,
            "status": status,
            "value": {
                "type": "json",
                "value": normalize_value(status, detail),
            },
        })
    return {"schema_version": REPORT_SCHEMA, "observations": observations}


def main(argv=None):
    parser = argparse.ArgumentParser(description="migration integration runner")
    parser.add_argument("--side", required=True, choices=("source", "target"))
    parser.add_argument("--repo", default=None)
    parser.add_argument("--root", default=None)
    parser.add_argument("--out", default=None)
    # The runtime harness invokes ``<side>/runner <report>`` (a positional
    # report path). Accept it in addition to ``--out`` so both call shapes work.
    parser.add_argument("report", nargs="?", default=None)
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root) if args.root else asset_root()
    repo = resolve_repo(args.repo)
    report = build_report(root, args.side, repo)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    dest = args.out or args.report
    if dest:
        dest = os.path.abspath(dest)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

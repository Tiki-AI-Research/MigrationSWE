#!/usr/bin/env python3
"""Hard gate for the express-fileupload -> Koa 2 middleware migration task.

The gate is a pure boolean safety/completeness boundary. It never decides how
complete the migration is (that is the verifier's job); it decides whether the
candidate is a legitimate, buildable, contract-preserving migration at all.

Every atom writes its own evidence file next to the gate report and the six
composite gates use conjunctive semantics: any applicable atom failing fails the
composite, and any failing atom fails the gate. Anti-cheating atoms mark the
candidate as cheating so the grader can apply the reward = -1 branch.

``p2p_regression_preserved`` owns existing-behaviour preservation: P2P is a
boolean behavioural floor and never a score, so it lives here (not in the
verifier) and never enters ``test_reward``. Its per-node outcome is recorded in
the report as ``p2p_nodes`` so the verifier can carry the platform's
``pass_to_pass`` identity mapping without scoring it.

Exit codes:
    0  report written (inspect ``success`` / ``hard_gate_success``)
    2  infrastructure error (report still written, status=infrastructure_error)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import sandbox
import semantic_scan

SCHEMA_VERSION = "migration-hard-gate/v1"

CATEGORY_ATOMS: "Dict[str, List[Tuple[str, bool]]]" = {
    "candidate_valid": [
        ("patch_present", True),
        ("patch_applicable", True),
    ],
    "target_build_valid": [
        ("build_pass", True),
        ("dependency_resolved", True),
    ],
    "migration_integrity_preserved": [
        ("no_deceptive_source_fallback", True),
        ("public_contract_preserved", True),
        # Conditional atom: reported ``na`` only on an explicit boolean ``false``
        # declaration (see main()); anything else fails closed.
        ("critical_data_preserved", False),
    ],
    # P2P is the behavioral floor: a migration that keeps the target framework
    # happy but changes existing observable behaviour is a regression, which is
    # a hard-gate concern (boolean, never scored). It stays out of the verifier
    # and out of ``test_reward`` so the two never double-count.
    "p2p_regression_preserved": [
        ("p2p_no_regression", True),
    ],
    "test_integrity": [
        ("tests_not_tampered", True),
        ("no_new_skip_or_xfail", True),
    ],
    "evaluation_environment_integrity": [
        ("protected_paths_unchanged", True),
        ("anti_cheating_pass", True),
        ("cross_trial_residue_absent", True),
    ],
}

# Atoms whose failure is a deliberate integrity violation (reward = -1 branch).
# ``tests_not_tampered`` is included only when the protected-test edit is a real
# assertion/fixture rewrite. A patch whose only test-surface change is a new
# skip/only/xfail marker is an ordinary hard-gate failure (reward = 0) and is
# reported by ``no_new_skip_or_xfail`` instead; see ``skip_only_test_change``.
CHEAT_ATOMS = {
    ("migration_integrity_preserved", "no_deceptive_source_fallback"),
    ("evaluation_environment_integrity", "anti_cheating_pass"),
    ("test_integrity", "tests_not_tampered"),
}

#: Environment variables that must never be switched on by the activation
#: probe: they select the harness/runtime mode itself, not a candidate feature
#: flag.
ACTIVATION_ENV_DENY = {
    "NODE_ENV", "NODE_PATH", "PATH", "HOME", "PWD", "SHELL", "USER", "LOGNAME",
    "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
}

#: Prefixes that must never be switched on either: the runtime loader and the
#: dynamic-linker variables change how the Node process itself starts, so a
#: "flag" that folded to one of them would break the probe instead of activating
#: a candidate branch.
ACTIVATION_ENV_DENY_PREFIXES = (
    "MIGRATION_", "NODE_", "NPM_", "LD_", "PYTHON", "DYLD_",
)

SKIP_PATTERNS = [
    re.compile(r"\b(?:it|describe|context|specify)\s*\.\s*(?:skip|only)\b"),
    re.compile(r"\b(?:xit|xtest|xdescribe|xcontext)\s*\("),
    re.compile(r"\bthis\s*\.\s*skip\s*\("),
    re.compile(r"\.\s*pending\s*\("),
    re.compile(r"\bxfail\b"),
    re.compile(r"\bmocha\s*\.\s*(?:skip|only)\b"),
]

# Applied to a whitespace/comment-normalised line, so ``it . skip (`` and
# ``{ skip: true }`` cannot hide behind formatting.
SKIP_PATTERNS_NORMALIZED = [
    re.compile(r"\b(?:it|describe|context|specify)\.(?:skip|only)\b"),
    re.compile(r"\b(?:xit|xtest|xdescribe|xcontext)\("),
    re.compile(r"\bthis\.skip\("),
    re.compile(r"\.pending\("),
    re.compile(r"\bxfail\b"),
    re.compile(r"\bmocha\.(?:skip|only)\b"),
    re.compile(r"\bskip\s*:\s*true\b"),
    re.compile(r"\bonly\s*:\s*true\b"),
]

_module_reference_patterns = semantic_scan.module_reference_patterns
_built_module_name_patterns = semantic_scan.built_module_name_patterns

# The anti-cheating pattern registry lives in ``semantic_scan`` so the
# integrity scanner and the verifier's completeness scanner can never drift
# apart (QC item 116).
STATIC_CHEAT_PATTERNS = semantic_scan.build_static_cheat_patterns()


# -- P2P (existing-behaviour regression) helpers ----------------------------
def normalize_title(title: str) -> str:
    """Canonical node-id normalization; must stay in sync with
    authoring/generate_verifier_manifests.py and verifier.py."""
    return re.sub(r"[^0-9A-Za-z]+", "_", title).strip("_").lower()


def assign_ids(records: "Sequence[Dict[str, Any]]") -> "List[Tuple[str, Dict[str, Any]]]":
    """Map real mocha events to stable node ids, keeping duplicates distinct."""
    from collections import Counter

    base_ids = [normalize_title(record.get("fullTitle") or record.get("full_title") or "") for record in records]
    counts = Counter(base_ids)
    seen: Counter = Counter()
    assigned: "List[Tuple[str, Dict[str, Any]]]" = []
    for base_id, record in zip(base_ids, records):
        seen[base_id] += 1
        node_id = base_id if counts[base_id] == 1 else f"{base_id}#{seen[base_id]}"
        assigned.append((node_id, record))
    return assigned


class Gate:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config: Dict[str, Any] = {}
        self.repo = Path(args.repo).resolve()
        self.tests_dir = Path(args.tests_dir).resolve() if args.tests_dir else Path(__file__).resolve().parent.parent
        self.logs_dir = Path(args.logs_dir).resolve() if args.logs_dir else Path("/logs/verifier")
        self.evidence_dir = self.logs_dir / "reports" / "hardgate"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.results: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "task_id": None,
            "base_commit": None,
            "status": "not_run",
            "success": False,
            "hard_gate_success": 0,
            "hard_gate_pass": False,
            "cheating_detected": False,
            "applicable_atoms": 0,
            "passed_atoms": 0,
            "failed_atoms": [],
            "any_atom_not_run": False,
            "infrastructure_error": None,
            "checks": {},
        }
        for category, atoms in CATEGORY_ATOMS.items():
            self.results["checks"][category] = {
                "status": "not_run",
                "atoms": {
                    atom: {"status": "not_run", "applicable": applicable, "evaluated": False, "reason": None, "evidence_ref": None}
                    for atom, applicable in atoms
                },
            }
        self.changed_paths: List[str] = []
        self.untracked_extras: List[str] = []
        self.patch_text = ""

    # -- evidence helpers ---------------------------------------------------
    def set_atom(
        self,
        category: str,
        atom: str,
        status: str,
        reason: str = "",
        evidence: Optional[Dict[str, Any]] = None,
        evaluated: bool = True,
    ) -> None:
        payload = {
            "schema_version": "migration-hard-gate-atom/v1",
            "task_id": self.results.get("task_id"),
            "category": category,
            "atom": atom,
            "status": status,
            "evaluated": evaluated,
            "reason": reason,
            "evidence": evidence or {},
        }
        target = self.evidence_dir / f"{category}__{atom}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        node = self.results["checks"][category]["atoms"][atom]
        node["status"] = status
        node["evaluated"] = evaluated
        node["reason"] = reason or None
        node["evidence_ref"] = str(target)

    def pass_atom(self, category: str, atom: str, reason: str = "", evidence: Optional[Dict[str, Any]] = None) -> None:
        self.set_atom(category, atom, "pass", reason, evidence)

    def fail_atom(self, category: str, atom: str, reason: str, evidence: Optional[Dict[str, Any]] = None,
                  evaluated: bool = True) -> None:
        self.set_atom(category, atom, "fail", reason, evidence, evaluated=evaluated)

    def cascade_atom(self, category: str, atom: str, reason: str) -> None:
        """Mark an atom as not run because an earlier atom failed the gate."""
        self.set_atom(category, atom, "not_run", reason, evaluated=False)

    def na_atom(self, category: str, atom: str, reason: str) -> None:
        self.set_atom(category, atom, "na", reason)

    def set_infrastructure_error(self, message: str) -> None:
        self.results["infrastructure_error"] = message

    # -- generic helpers ----------------------------------------------------
    def run(self, cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
            timeout: int = 120, output_limit: int = 8000) -> Dict[str, Any]:
        """Run an evaluator command inside the private network namespace.

        Commands that execute candidate code are already wrapped by
        ``sandbox.wrap`` (namespaces + capability drop); everything else is a
        pure evaluator command (``git``, ``node --check``, dependency
        resolution) and only gets the namespace barrier, keeping root so it can
        still stage scratch trees and read the frozen assets.
        """
        execution = list(cmd) if cmd and cmd[0] == "unshare" else sandbox.network_isolated_command(cmd)
        merged = os.environ.copy()
        if env:
            merged.update(env)
        if sandbox.is_candidate_command(execution):
            merged = sandbox.candidate_env(merged, extra=env)
        try:
            proc = subprocess.run(
                execution,
                cwd=str(cwd) if cwd else None,
                env=merged,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
            result = {
                "cmd": cmd,
                "execution_command": execution,
                "network_isolated": sandbox.isolation_supported(),
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-output_limit:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-output_limit:],
                "timed_out": False,
            }
            result["sandbox_error"] = sandbox.namespace_error(result)
            return result
        except FileNotFoundError as err:
            return {"cmd": cmd, "execution_command": execution, "returncode": None, "stdout": "",
                    "stderr": str(err), "timed_out": False, "missing": True}
        except subprocess.TimeoutExpired as err:
            return {
                "cmd": cmd,
                "execution_command": execution,
                "returncode": None,
                "stdout": (err.stdout or b"").decode("utf-8", "replace")[-4000:],
                "stderr": (err.stderr or b"").decode("utf-8", "replace")[-4000:],
                "timed_out": True,
            }

    def node_bin(self) -> str:
        return self.args.node or "node"

    def node_env(self, scratch: Optional[Path] = None) -> Dict[str, str]:
        parts: List[str] = []
        deps = self.tests_dir / "deps" / "node_modules"
        if deps.is_dir():
            parts.append(str(deps))
        repo_modules = self.repo / "node_modules"
        if repo_modules.is_dir():
            parts.append(str(repo_modules))
        if scratch is not None:
            scratch_modules = scratch / "node_modules"
            if scratch_modules.is_dir():
                parts.append(str(scratch_modules))
        env = {"NODE_ENV": "test"}
        if parts:
            env["NODE_PATH"] = os.pathsep.join(parts)
        return env

    def hard_gate_conf(self) -> Dict[str, Any]:
        conf = self.config.get("hard_gate")
        return conf if isinstance(conf, dict) else {}

    def activation_env(self) -> Dict[str, str]:
        """Env-gated source fallbacks must be *executed*, not skipped over.

        A candidate can hide an Express/``child_process`` fallback behind
        ``process.env.SOMETHING === '1'``; the default path never triggers that
        branch, so neither the ``Module._load`` hook nor the subprocess hook
        ever sees the load (QC RC-1). Every environment variable that the
        candidate's own added code reads as a flag is therefore switched on for
        the runtime probe.
        """
        added = "\n".join(
            line[1:] for line in self.patch_text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        code = semantic_scan.strip_js_noise(added)
        names = set()
        for match in re.finditer(
            r"""process\s*\.\s*env\s*(?:\.\s*([A-Za-z_$][\w$]*)|\[\s*['"]([\w$]+)['"]\s*\])""",
            code,
        ):
            names.add(match.group(1) or match.group(2))
        # A flag whose *name* is built at runtime (``env[['LEGACY','SOURCE',
        # 'MODE'].join('_')]``) is invisible to the literal scan above. The
        # same literal folding the specifier decoder uses resolves it, so the
        # gated branch is still executed by the probe instead of being skipped
        # over (QC ADV-DYNAMIC-FALLBACK).
        names.update(semantic_scan.computed_env_names(code))
        return {
            name: "1"
            for name in sorted(names)
            if name
            and name not in ACTIVATION_ENV_DENY
            and not name.startswith(ACTIVATION_ENV_DENY_PREFIXES)
        }

    def option_defaults(self) -> Dict[str, Any]:
        """The frozen ``express_surface.option_defaults`` baseline (item 53)."""
        path = self.tests_dir / "assets" / "public_contract.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        surface = doc.get("express_surface") or {}
        defaults = surface.get("option_defaults")
        return dict(defaults) if isinstance(defaults, dict) else {}

    # -- patch/scratch ------------------------------------------------------
    def parse_changed_paths_from_patch(self) -> List[str]:
        paths: List[str] = []
        seen = set()
        for line in self.patch_text.splitlines():
            if not line.startswith("diff --git "):
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            candidate = parts[3]
            if candidate == "/dev/null":
                candidate = parts[2]
            if candidate.startswith("b/") or candidate.startswith("a/"):
                candidate = candidate[2:]
            candidate = candidate.strip('"')
            if candidate and candidate not in seen:
                seen.add(candidate)
                paths.append(candidate)
        return paths

    def prepare_scratch(self) -> Tuple[Optional[Path], Optional[str]]:
        base_commit = self.args.base_commit or self.config.get("base_commit")
        if not base_commit:
            return None, "config.base_commit is missing"
        self.results["base_commit"] = base_commit

        scratch = Path(tempfile.mkdtemp(prefix="migration-hardgate-"))
        # Base identity is derived from *content*, never from the candidate
        # checkout's git metadata: a candidate can rewrite .git/refs and claim
        # any commit, so the git objects are never consulted here. Two harness
        # shapes are supported and both are proven against the frozen base
        # manifest that ships with the (read-only) verifier assets:
        #   A. the worktree already contains the candidate patch -> reverse-apply
        #      the patch to recover the base tree;
        #   B. the worktree is already the pristine base -> use it as-is.
        method = "reverse-apply candidate patch, then compare with the frozen base manifest"
        try:
            self.stage_candidate_tree(scratch)
        except OSError as err:
            return None, f"cannot stage the candidate worktree: {err}"
        # ``git apply`` behaves deterministically inside a repository, and the
        # scratch repository is also what the later atoms diff against.
        self.run(["git", "init", "-q"], cwd=scratch, timeout=60)
        reverse = self.run(
            ["git", "apply", "-R", "--binary", "--whitespace=nowarn", str(Path(self.args.patch_file).resolve())],
            cwd=scratch, timeout=180,
        )
        if reverse["returncode"] != 0:
            # Shape B: the candidate patch is not applied yet. Re-stage from
            # scratch so a partially applied patch can never leak into the base.
            method = "worktree already equals the frozen base manifest (candidate patch not applied)"
            shutil.rmtree(scratch, ignore_errors=True)
            scratch.mkdir(parents=True, exist_ok=True)
            try:
                self.stage_candidate_tree(scratch)
            except OSError as err:
                return None, f"cannot stage the candidate worktree: {err}"
            self.run(["git", "init", "-q"], cwd=scratch, timeout=60)
        try:
            identity_ok, identity = self.verify_base_identity(scratch)
        except RuntimeError as err:
            return None, str(err)
        identity["method"] = method
        self.results["base_identity"] = identity
        if not identity_ok:
            return None, (
                "candidate worktree does not match the frozen base manifest and the candidate patch could not be "
                "reverse-applied, so the base tree cannot be reconstructed: "
                + json.dumps(
                    {"reverse_apply_rc": reverse["returncode"],
                     "reverse_apply_stderr": (reverse.get("stderr") or "")[-300:],
                     "missing": identity["missing"][:10],
                     "mismatches": identity["mismatches"][:10]},
                    ensure_ascii=False,
                )
            )

        # Patch closure (QC RC-PATCH-CLOSURE): the platform collects the
        # deliverable with ``git diff <base> HEAD``, which never contains
        # untracked files. The candidate worktree may still carry extras, and
        # ``git add -A`` below would commit them into the gate's synthetic base,
        # letting them participate in grading while staying invisible in
        # model.patch and outside the allowed/protected-path audits. The graded
        # tree is therefore reconstructed as ``base + model.patch`` only: every
        # extra file is dropped and recorded so the verdict stays reproducible
        # from the archived patch alone.
        self.untracked_extras = self.find_untracked_extras(scratch)
        for rel in self.untracked_extras:
            try:
                (scratch / rel).unlink()
            except OSError:
                pass
        if self.untracked_extras:
            self.results["untracked_paths_dropped"] = list(self.untracked_extras)
            self.results["untracked_paths_dropped_count"] = len(self.untracked_extras)
            identity["untracked_paths_dropped"] = self.untracked_extras[:50]

        repo_modules = self.repo / "node_modules"
        if repo_modules.is_dir():
            try:
                os.symlink(repo_modules, scratch / "node_modules", target_is_directory=True)
            except OSError:
                pass
        self.run(["git", "add", "-A"], cwd=scratch, timeout=120)
        self.run(
            ["git", "-c", "user.email=hardgate@local", "-c", "user.name=hardgate", "commit", "-q", "-m", "base"],
            cwd=scratch, timeout=120,
        )
        # The probe runs as the unprivileged sandbox identity, so the staged
        # candidate must be readable by it. ``a+rX`` never touches the execute
        # bit of a regular file, so the staged git index stays clean.
        sandbox.open_tree(scratch)
        return scratch, None

    # -- base identity (content hashes, never candidate git metadata) --------
    def base_manifest(self) -> Dict[str, str]:
        """Frozen path -> sha256 map of the base commit (verifier-side asset)."""
        path = self.tests_dir / "assets" / "base_tree.sha256.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(f"cannot read frozen base manifest {path}: {err}") from err
        files = doc.get("files")
        if not isinstance(files, dict) or not files:
            raise RuntimeError(f"frozen base manifest {path} carries no file hashes")
        return {str(key): str(value) for key, value in files.items()}

    def stage_candidate_tree(self, scratch: Path) -> None:
        """Copy the candidate worktree without .git / node_modules."""
        ignore = shutil.ignore_patterns(".git", "node_modules", "__pycache__", "*.pyc")
        shutil.copytree(self.repo, scratch, symlinks=True, ignore=ignore, dirs_exist_ok=True)

    def verify_base_identity(self, scratch: Path) -> Tuple[bool, Dict[str, Any]]:
        manifest = self.base_manifest()
        missing: List[str] = []
        mismatches: List[Dict[str, str]] = []
        for rel, expected in manifest.items():
            target = scratch / rel
            if not target.is_file():
                missing.append(rel)
                continue
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected:
                mismatches.append({"path": rel, "expected": expected, "actual": actual})
        evidence: Dict[str, Any] = {
            "method": "reverse-apply candidate patch, then compare with the frozen base manifest",
            "manifest": "assets/base_tree.sha256.json",
            "base_commit": self.results.get("base_commit"),
            "checked_files": len(manifest),
            "missing": missing[:50],
            "mismatches": mismatches[:50],
        }
        return (not missing and not mismatches), evidence

    def scratch_changed_paths(self, scratch: Path) -> List[str]:
        paths: List[str] = []
        for cmd in (
            ["git", "diff", "--name-only", "-z", "HEAD"],
            ["git", "ls-files", "-z", "--others", "--exclude-standard"],
        ):
            result = self.run(cmd, cwd=scratch, timeout=120)
            for raw in (result.get("stdout") or "").split("\0"):
                item = raw.strip()
                if item and item not in paths:
                    paths.append(item)
        return paths

    #: Files that are created by tooling rather than authored by the candidate:
    #: they are not part of the deliverable and their absence from the base
    #: manifest is expected.
    NON_DELIVERABLE_SUFFIXES = (".pyc", ".pyo", ".orig", ".rej")

    def find_untracked_extras(self, scratch: Path) -> List[str]:
        """Files the candidate worktree carries that its patch does not declare.

        The frozen base manifest is exhaustive for the base commit, so anything
        present in the reconstructed base tree that the manifest does not list
        (and that the candidate patch does not add) came from the untracked
        workspace. It can never be part of ``model.patch`` and is therefore
        dropped before grading (QC RC-PATCH-CLOSURE).
        """
        manifest = self.base_manifest()
        extras: List[str] = []
        for path in sorted(scratch.rglob("*")):
            if not path.is_file() and not path.is_symlink():
                continue
            rel = path.relative_to(scratch).as_posix()
            if rel.split("/", 1)[0] in (".git", "node_modules"):
                continue
            if rel in manifest:
                continue
            if "__pycache__" in rel.split("/") or rel.endswith(self.NON_DELIVERABLE_SUFFIXES):
                continue
            extras.append(rel)
        return extras

    # -- atoms --------------------------------------------------------------
    def atom_patch_present(self) -> bool:
        patch_path = Path(self.args.patch_file)
        if not patch_path.is_file():
            self.fail_atom("candidate_valid", "patch_present", f"patch file not found: {patch_path}")
            return False
        data = patch_path.read_bytes()
        self.patch_text = data.decode("utf-8", "replace")
        sha = hashlib.sha256(data).hexdigest()
        if len(data) == 0:
            self.fail_atom("candidate_valid", "patch_present", "patch file is empty", {"size": 0})
            return False
        if "diff --git" not in self.patch_text or "@@" not in self.patch_text:
            self.fail_atom(
                "candidate_valid",
                "patch_present",
                "patch file does not contain a unified diff",
                {"size": len(data), "sha256": sha},
            )
            return False
        self.pass_atom(
            "candidate_valid",
            "patch_present",
            "patch file present and parses as a unified diff",
            {"size": len(data), "sha256": sha, "declared_paths": self.parse_changed_paths_from_patch()},
        )
        return True

    def atom_patch_applicable(self, scratch: Path) -> bool:
        patch_arg = str(Path(self.args.patch_file).resolve())
        check = self.run(["git", "apply", "--check", "--binary", "--whitespace=nowarn", patch_arg], cwd=scratch, timeout=120)
        if check["returncode"] != 0:
            self.fail_atom(
                "candidate_valid",
                "patch_applicable",
                "patch does not apply cleanly to the frozen base commit",
                {"stdout": check["stdout"], "stderr": check["stderr"]},
            )
            return False
        apply_result = self.run(["git", "apply", "--binary", "--whitespace=nowarn", patch_arg], cwd=scratch, timeout=120)
        if apply_result["returncode"] != 0:
            self.fail_atom(
                "candidate_valid",
                "patch_applicable",
                "patch passed --check but failed to apply",
                {"stdout": apply_result["stdout"], "stderr": apply_result["stderr"]},
            )
            return False
        self.changed_paths = self.scratch_changed_paths(scratch)
        evidence: Dict[str, Any] = {"changed_paths": self.changed_paths}
        if self.untracked_extras:
            # The graded tree is closed over ``model.patch``: untracked workspace
            # files were dropped instead of being absorbed into the synthetic
            # base, so the report has to say which ones and why.
            evidence["dropped_untracked_paths"] = self.untracked_extras[:50]
            evidence["patch_closure_note"] = (
                "untracked files are not part of `git diff <base> HEAD`; they were dropped so "
                "the graded tree is exactly base + model.patch"
            )
        self.pass_atom(
            "candidate_valid",
            "patch_applicable",
            "patch applies cleanly with git apply --check/--binary",
            evidence,
        )
        return True

    def atom_build_pass(self, scratch: Path) -> bool:
        conf = self.hard_gate_conf()
        compile_paths = conf.get("compile_paths") or ["lib"]
        targets: List[Path] = []
        for root in compile_paths:
            base = scratch / root
            if base.is_file() and base.suffix == ".js":
                targets.append(base)
            elif base.is_dir():
                targets.extend(sorted(p for p in base.rglob("*.js") if p.is_file()))
        if not targets:
            self.fail_atom("target_build_valid", "build_pass", "no JavaScript build targets found under compile_paths", {"compile_paths": compile_paths})
            return False

        per_file: List[Dict[str, Any]] = []
        failed: List[str] = []
        timed_out: List[str] = []
        for target in targets:
            result = self.run([self.node_bin(), "--check", str(target)], cwd=scratch, timeout=60)
            rel = str(target.relative_to(scratch))
            per_file.append({
                "file": rel,
                "returncode": result["returncode"],
                "stderr": result["stderr"],
                "timed_out": bool(result.get("timed_out")),
                "missing": bool(result.get("missing")),
            })
            if result.get("timed_out") or result.get("missing"):
                timed_out.append(rel)
            elif result["returncode"] != 0:
                failed.append(rel)

        # A watchdog timeout or a missing interpreter is an evaluator problem, not
        # a candidate failure: attributing it to the candidate would turn an
        # overloaded machine into a deterministic "you broke the build".
        load = self.run(
            sandbox.wrap([self.node_bin(), "-e", "require('./lib/index.js');"]),
            cwd=scratch, env=self.node_env(scratch), timeout=60,
        )
        if timed_out or load.get("timed_out") or load.get("missing"):
            detail = {
                "timed_out_files": timed_out,
                "load_timed_out": bool(load.get("timed_out")),
                "load_missing": bool(load.get("missing")),
                "load_stderr": load.get("stderr", "")[-300:],
                "files_checked": len(targets),
            }
            message = "build check did not finish inside the watchdog window (timeout/missing interpreter)"
            self.set_infrastructure_error(f"{message}: {json.dumps(detail, ensure_ascii=False)}")
            self.fail_atom("target_build_valid", "build_pass", message, detail, evaluated=False)
            return False
        if failed or load["returncode"] != 0:
            self.fail_atom(
                "target_build_valid",
                "build_pass",
                "target sources failed syntax/load checks",
                {"syntax_failures": failed, "load": load, "files_checked": len(targets)},
            )
            return False
        self.pass_atom(
            "target_build_valid",
            "build_pass",
            f"{len(targets)} JavaScript targets pass `node --check` and lib/index.js loads",
            {
                "files_checked": len(targets),
                "load_stdout": load["stdout"],
                "sandbox": sandbox.describe(),
            },
        )
        return True

    def atom_dependency_resolved(self, scratch: Path) -> bool:
        conf = self.hard_gate_conf()
        expected_major = int(conf.get("expected_koa_major", 2))
        forbidden_source = conf.get("forbidden_source_modules") or ["express", "body-parser", "multer"]

        resolve = self.run(
            [self.node_bin(), "-e", "process.stdout.write(String(require('koa/package.json').version))"],
            cwd=scratch,
            env=self.node_env(scratch),
            timeout=60,
        )
        koa_version = (resolve.get("stdout") or "").strip()
        koa_ok = resolve["returncode"] == 0 and koa_version.startswith(f"{expected_major}.")

        # The candidate must not change runtime dependencies: dependencies are
        # frozen against the pristine base commit (no new source-framework or
        # bridging dependency may sneak in through package.json).
        package_ok = True
        package_evidence: Dict[str, Any] = {}
        base_package_result = self.run(["git", "show", "HEAD:package.json"], cwd=scratch, timeout=60)
        package_path = scratch / "package.json"
        if not package_path.is_file():
            package_ok = False
            package_evidence = {"error": "package.json missing from the candidate tree"}
        elif base_package_result["returncode"] != 0:
            package_ok = False
            package_evidence = {"error": "cannot read the frozen base package.json", "stderr": base_package_result["stderr"]}
        else:
            try:
                package = json.loads(package_path.read_text(encoding="utf-8"))
                base_package = json.loads(base_package_result["stdout"])
            except Exception as err:  # noqa: BLE001
                package = {}
                base_package = {}
                package_ok = False
                package_evidence = {"error": f"package.json is not valid JSON: {err}"}
            if package and base_package:
                # ``devDependencies`` matter too: a candidate could hide a bridge
                # dependency (or downgrade the test toolchain) there while keeping
                # ``dependencies`` byte-identical.
                added: List[str] = []
                removed: List[str] = []
                changed_ranges: List[str] = []
                offenders: List[str] = []
                sections: Dict[str, Any] = {}
                for section in ("dependencies", "devDependencies"):
                    current = package.get(section) or {}
                    base = base_package.get(section) or {}
                    section_added = sorted(set(current) - set(base))
                    section_removed = sorted(set(base) - set(current))
                    section_changed = sorted(
                        name for name in set(current) & set(base) if current[name] != base[name]
                    )
                    # ``express``/``supertest`` are legitimate *dev* dependencies of
                    # the frozen test toolchain; the "must not add the target or
                    # source framework" rule is about the runtime dependency set.
                    banned = ({"koa", "supertest"} | set(forbidden_source)) if section == "dependencies" else set()
                    section_offenders = sorted(set(current) & banned)
                    added += [f"{section}:{name}" for name in section_added]
                    removed += [f"{section}:{name}" for name in section_removed]
                    changed_ranges += [f"{section}:{name}" for name in section_changed]
                    offenders += [f"{section}:{name}" for name in section_offenders]
                    sections[section] = {
                        "current": current,
                        "base": base,
                        "added": section_added,
                        "removed": section_removed,
                        "changed_ranges": section_changed,
                        "offenders": section_offenders,
                    }
                package_evidence = {"sections": sections, "added": added, "removed": removed,
                                    "changed_ranges": changed_ranges, "offenders": offenders}
                package_ok = not added and not removed and not changed_ranges and not offenders

        deps_manifest = self.tests_dir / "deps" / "package.json"
        deps_installed = self.tests_dir / "deps" / "node_modules" / "koa" / "package.json"
        deps_lock = self.tests_dir / "deps" / "package-lock.json"
        lock_evidence: Dict[str, Any] = {}
        lock_ok = True
        if deps_manifest.is_file() and deps_installed.is_file():
            installed = json.loads(deps_installed.read_text(encoding="utf-8")).get("version")
            declared_dep = (json.loads(deps_manifest.read_text(encoding="utf-8")).get("dependencies") or {}).get("koa")
            locked = None
            if deps_lock.is_file():
                lock = json.loads(deps_lock.read_text(encoding="utf-8"))
                locked = (lock.get("packages") or {}).get("node_modules/koa", {}).get("version")
            lock_evidence = {"declared": declared_dep, "locked": locked, "installed": installed}
            lock_ok = installed == locked == koa_version
        else:
            lock_ok = False
            lock_evidence = {"declared_manifest": str(deps_manifest), "installed_manifest": str(deps_installed)}

        if koa_ok and package_ok and lock_ok:
            self.pass_atom(
                "target_build_valid",
                "dependency_resolved",
                f"Koa {koa_version} resolves in the target environment and no runtime dependency was added or changed",
                {"koa_version": koa_version, "package": package_evidence, "deps_lock": lock_evidence},
            )
            return True
        self.fail_atom(
            "target_build_valid",
            "dependency_resolved",
            "target dependency resolution failed",
            {
                "koa_version": koa_version,
                "koa_probe": {"returncode": resolve["returncode"], "stderr": resolve["stderr"]},
                "package": package_evidence,
                "deps_lock": lock_evidence,
            },
        )
        return False

    @staticmethod
    def _protected(protected: "Sequence[str]", normalized: str) -> bool:
        for entry in protected:
            prefix = str(entry).rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
        return False

    def added_skip_markers(self) -> List[str]:
        """Added lines that introduce a skip/only/xfail marker as code.

        Comments and string/template contents are blanked first, matching
        ``atom_no_new_skip_or_xfail``: a title that merely mentions ``it.skip``
        is not a skipped test.
        """
        added_raw = [
            line[1:] for line in self.patch_text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        added = semantic_scan.strip_js_noise("\n".join(added_raw), strings=True).split("\n")
        offenders: List[str] = []
        for raw, line in zip(added_raw, added):
            if any(pattern.search(line) for pattern in SKIP_PATTERNS):
                offenders.append(raw.strip()[:200])
                continue
            normalized = self.significant_line(line)
            if any(pattern.search(normalized) for pattern in SKIP_PATTERNS_NORMALIZED):
                offenders.append(raw.strip()[:200])
        return offenders

    def skip_only_test_change(self, path: str) -> bool:
        """True when a protected test file's only behavioural delta is skip/only/xfail.

        Weakened assertions, deleted cases and fixture rewrites stay
        ``behavioral_change`` and remain a cheating signal. A file that only
        gains a skip/only/xfail marker is the ordinary ``no_new_skip_or_xfail``
        failure and must not share the reward = -1 branch.
        """
        normalized = path.replace("\\", "/").lstrip("./")
        removed: List[str] = []
        added: List[str] = []
        in_file = False
        for line in self.patch_text.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                target = parts[3] if len(parts) >= 4 else ""
                if target.startswith(("a/", "b/")):
                    target = target[2:]
                in_file = target.strip('"') == normalized
                continue
            if line.startswith("--- "):
                target = line[4:].split("\t")[0].strip()
                if target.startswith(("a/", "b/")):
                    target = target[2:]
                in_file = target.strip('"') == normalized
                continue
            if not in_file or line.startswith(("+++", "@@")):
                continue
            if line.startswith("-"):
                removed.append(line[1:])
            elif line.startswith("+"):
                added.append(line[1:])
        if not added:
            return False
        # Compare the raw lines first so ``it(`` -> ``it.skip(`` can be paired
        # before whitespace folding. Anything left over is a real edit.
        pending_removed = [line for line in removed if self.significant_line(line)]
        unpaired_added = []
        markers = 0
        for line in added:
            if not self.significant_line(line):
                continue
            paired = False
            comparable = semantic_scan.strip_js_noise(line, strings=False)
            for index, old in enumerate(pending_removed):
                old_cmp = semantic_scan.strip_js_noise(old, strings=False)
                if self._skip_marker_pair(old_cmp, comparable):
                    pending_removed.pop(index)
                    paired = True
                    markers += 1
                    break
            if not paired:
                unpaired_added.append(line)
        if pending_removed:
            return False
        stripped = semantic_scan.strip_js_noise("\n".join(unpaired_added), strings=True)
        for line in stripped.split("\n"):
            if not line.strip():
                continue
            normalized_line = self.significant_line(line)
            if not normalized_line:
                continue
            if any(pattern.search(line) for pattern in SKIP_PATTERNS) or any(
                pattern.search(normalized_line) for pattern in SKIP_PATTERNS_NORMALIZED
            ):
                markers += 1
                continue
            return False
        return markers > 0

    @staticmethod
    def _skip_marker_pair(removed: str, added: str) -> bool:
        """True when ``added`` is ``removed`` plus a skip/only/xfail marker.

        ``it(`` rewritten as ``it.skip(`` (or ``it . skip (``) is one marker,
        not a second behavioural edit. The rest of the line must still match.
        """
        def marked(line: str) -> bool:
            return any(pattern.search(line) for pattern in SKIP_PATTERNS) or any(
                pattern.search(line) for pattern in SKIP_PATTERNS_NORMALIZED
            )
        if marked(removed) or not marked(added):
            return False
        stripped = added
        stripped = re.sub(r"\.\s*(?:skip|only|pending)\b", "", stripped)
        stripped = re.sub(
            r"\b(?:xit|xtest|xdescribe|xcontext)\s*\(",
            lambda m: m.group(0)[1:],
            stripped,
        )
        stripped = re.sub(r"\bxfail\b", "", stripped)
        return Gate.significant_line(stripped) == Gate.significant_line(removed)

    def atom_tests_not_tampered(self, scratch: Path) -> bool:
        protected = self.hard_gate_conf().get("protected_paths") or ["test", "tests", "fixtures", "coverage", ".nyc_output"]
        offenders = []
        skip_only_paths: List[str] = []
        content_classes: Dict[str, str] = {}
        for path in self.changed_paths:
            normalized = path.replace("\\", "/").lstrip("./")
            if self._protected(protected, normalized):
                klass = self.classify_patch_content(path)
                content_classes[path] = klass
                if klass != "no_op_refactor":
                    if self.skip_only_test_change(path):
                        skip_only_paths.append(path)
                    else:
                        offenders.append(path)

        # Path-level classification alone can be fooled (a renamed path, a
        # whitespace-normalised assertion, a skip smuggled in through a helper).
        # The frozen base manifest is the ground truth: every protected file that
        # exists in the base commit must still hash to its recorded value in the
        # candidate tree.
        manifest = self.base_manifest()
        content_mismatches: List[Dict[str, str]] = []
        for rel, expected in manifest.items():
            normalized = rel.replace("\\", "/")
            if not self._protected(protected, normalized):
                continue
            # A whitespace/comment-only edit is an explicit ``no_op_refactor``
            # (the class this gate already recognises); it changes the bytes but
            # not the behaviour the frozen hash is there to protect. Assertion
            # edits still classify as ``behavioral_change`` and still fail.
            # A skip/only/xfail-only addition also changes the hash, but that
            # signal belongs to ``no_new_skip_or_xfail`` (ordinary failure), not
            # to this cheating atom.
            if content_classes.get(rel) == "no_op_refactor" or rel in skip_only_paths:
                continue
            target = scratch / rel
            if not target.is_file():
                content_mismatches.append({"path": rel, "reason": "missing from the candidate tree"})
                continue
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected:
                content_mismatches.append({"path": rel, "expected": expected, "actual": actual})

        if offenders or content_mismatches:
            self.fail_atom(
                "test_integrity",
                "tests_not_tampered",
                "candidate modified protected test/fixture paths (path classification and frozen hashes)",
                {
                    "offenders": offenders,
                    "content_classes": content_classes,
                    "hash_mismatches": content_mismatches[:50],
                    "skip_only_paths": skip_only_paths,
                    "manifest": "assets/base_tree.sha256.json",
                },
            )
            return False
        self.pass_atom(
            "test_integrity",
            "tests_not_tampered",
            "every protected test/fixture file still matches the frozen base hash"
            if not skip_only_paths
            else "protected test edits are skip/only/xfail markers only; not counted as tampering",
            {
                "changed_paths": self.changed_paths,
                "content_classes": content_classes,
                "skip_only_paths": skip_only_paths,
                "hashed_files": sum(1 for rel in manifest if self._protected(protected, rel.replace("\\", "/"))),
            },
        )
        return True

    def atom_no_new_skip_or_xfail(self) -> bool:
        # A comment or a test *title* that merely mentions ``it.skip`` is not a
        # skipped test (QC RC-2), so comments and string/template literal
        # contents are blanked before the marker scan. The real marker is code
        # (``it.skip(...)``), which survives the strip.
        # Semantic pass: ``it . skip (`` / ``skip: true`` / ``it /*x*/ . skip``
        # survive the literal scan, so the whitespace- and comment-normalised
        # form is scanned as well. A hit here is an ordinary hard-gate failure
        # (reward = 0), never the cheating branch.
        offenders = self.added_skip_markers()
        # A comment/whitespace-only touch of a spec file is a no-op refactor,
        # not a weakened test. A skip/only/xfail-only addition is already
        # captured by ``offenders`` above; counting that same file again would
        # make a pure marker look like a behavioural spec rewrite. Assertion
        # weakening and other non-marker edits still fail this atom.
        spec_changes = [
            p for p in self.changed_paths
            if (p.endswith((".spec.js", ".spec.ts")) or p.endswith(".mocharc.json"))
            and self.classify_patch_content(p) != "no_op_refactor"
            and not self.skip_only_test_change(p)
        ]
        if offenders or spec_changes:
            self.fail_atom(
                "test_integrity",
                "no_new_skip_or_xfail",
                "candidate added skip/only/xfail markers or edited spec/mocha configuration",
                {"offenders": offenders, "spec_changes": spec_changes},
            )
            return False
        self.pass_atom("test_integrity", "no_new_skip_or_xfail", "no skip/only/xfail marker or spec edit detected")
        return True

    def atom_protected_paths_unchanged(self, scratch: Path) -> bool:
        conf = self.hard_gate_conf()
        allowed = set(conf.get("allowed_paths") or [])
        immutable = conf.get("immutable_paths") or []

        outside_allowed: List[str] = []
        immutable_hits: List[str] = []
        escapes: List[str] = []
        for path in self.changed_paths:
            normalized = path.replace("\\", "/").lstrip("./")
            if normalized.startswith("../") or "/../" in normalized:
                escapes.append(path)
                continue
            allowed_hit = False
            for entry in allowed:
                prefix = str(entry).replace("\\", "/").lstrip("./")
                if prefix.endswith("/"):
                    allowed_hit = normalized == prefix.rstrip("/") or normalized.startswith(prefix)
                else:
                    allowed_hit = normalized == prefix
                if allowed_hit:
                    break
            if allowed and not allowed_hit:
                # Content-level distinction (HG-005): a comment/whitespace-only
                # touch of an out-of-scope file is an irrelevant refactor, not a
                # scope violation. Behavioural edits and dependency upgrades are.
                if self.classify_patch_content(path) != "no_op_refactor":
                    outside_allowed.append(path)
            for entry in immutable:
                prefix = entry.rstrip("/")
                if normalized == prefix or normalized.startswith(prefix + "/"):
                    immutable_hits.append(path)
                    break

        content_classes = {
            path: self.classify_patch_content(path)
            for path in list(outside_allowed) + list(immutable_hits)
        }

        tracked = self.run(["git", "ls-files", "-s"], cwd=scratch, timeout=60)
        symlink_paths = [
            parts[-1]
            for parts in (line.split() for line in (tracked.get("stdout") or "").splitlines())
            if len(parts) >= 4 and parts[0] == "120000"
        ]

        if outside_allowed or immutable_hits or escapes or symlink_paths:
            self.fail_atom(
                "evaluation_environment_integrity",
                "protected_paths_unchanged",
                "candidate changed paths outside the declared migration scope or introduced symlinks",
                {
                    "outside_allowed": outside_allowed,
                    "immutable_hits": immutable_hits,
                    "path_escapes": escapes,
                    "symlinks": symlink_paths,
                    "content_classes": content_classes,
                },
            )
            return False
        self.pass_atom(
            "evaluation_environment_integrity",
            "protected_paths_unchanged",
            "all changed paths stay inside the declared lib/ migration scope",
            {"changed_paths": self.changed_paths, "allowed_paths": sorted(allowed), "content_classes": content_classes},
        )
        return True

    # The bridge (L2 anti-cheating) labels come from the shared registry so a
    # new indirect-load pattern is classified in exactly one place (QC 116).
    BRIDGE_PATTERNS = set(semantic_scan.BRIDGE_LABELS)

    def allowed_read_paths(self) -> List[str]:
        """Evaluation assets a candidate is explicitly permitted to read."""
        conf = self.hard_gate_conf()
        return [str(path) for path in (conf.get("allowed_read_paths") or [])]

    def _is_allowed_read(self, reference: str) -> bool:
        allowed = self.allowed_read_paths()
        candidate = reference.rstrip("/") or "/"
        for entry in allowed:
            target = entry.rstrip("/") or "/"
            if candidate == target or candidate.startswith(target + "/"):
                return True
        return False

    def static_cheat_offenders(self, scratch: Optional[Path] = None) -> List[Dict[str, str]]:
        offenders: List[Dict[str, str]] = []
        added = [
            line[1:] for line in self.patch_text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        # Comments are documentation, not behaviour: strip them *before*
        # matching so a note that mentions ``require(child_process)`` is never
        # reported as a bridge call (QC 42/48). The strip runs over the whole
        # added-text so block comments spanning several lines are handled too.
        stripped = semantic_scan.strip_js_noise("\n".join(added)).split("\n")
        for content in stripped:
            for pattern, label in STATIC_CHEAT_PATTERNS:
                match = pattern.search(content)
                if not match:
                    continue
                if label == "references evaluation asset paths" and self._is_allowed_read(match.group(1)):
                    continue
                offenders.append({"line": content.strip()[:200], "pattern": label})
                break
        if scratch is None:
            offenders.extend(self.constructed_specifier_offenders("\n".join(stripped)))
        else:
            # Inspect complete files, not disconnected added lines: an alias or
            # decoded value may be defined in unchanged context or on another
            # line of the same file. Never join bindings across different files.
            for path in sorted((scratch / "lib").rglob("*")):
                if (not path.is_file() or path.is_symlink() or
                        path.suffix.lower() not in ("", ".js", ".cjs", ".mjs", ".jsx")):
                    continue
                source = path.read_text(encoding="utf-8", errors="replace")
                for entry in self.constructed_specifier_offenders(source):
                    offenders.append({**entry, "path": path.relative_to(scratch).as_posix()})
        return offenders

    def constructed_specifier_offenders(self, added_code: str) -> List[Dict[str, str]]:
        """Resolved forbidden specifiers actually passed to a module loader.

        A decoding operation or an unused loader alias alone is not evidence.
        The shared scanner follows constant values through direct, aliased,
        computed and multiline loads; the runtime audit covers unresolved code.
        """
        conf = self.hard_gate_conf()
        modules = [
            *(conf.get("forbidden_source_modules") or ["express", "body-parser", "multer"]),
            *(conf.get("forbidden_runtime_modules") or []),
        ]
        evidence = semantic_scan.module_load_findings(added_code, modules)
        return [
            {"line": f"line {entry['line']}: module specifier {entry['value']!r}", "pattern": entry['label']}
            for entry in evidence
        ]

    def static_offenders_split(self, scratch: Optional[Path] = None) -> Dict[str, List[Dict[str, str]]]:
        offenders = self.static_cheat_offenders(scratch)
        return {
            "bridge": [o for o in offenders if o["pattern"] in self.BRIDGE_PATTERNS],
            "tamper": [o for o in offenders if o["pattern"] not in self.BRIDGE_PATTERNS],
        }

    # -- content-level classification of a changed path (HG-005) ------------
    DEPENDENCY_FILES = {
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
    }

    @staticmethod
    def significant_line(line: str) -> str:
        """Strip whitespace and JS comments so no-op refactors compare equal.

        Both comment syntaxes count: a test file touched only by a ``//`` note
        *or* a ``/* ... */`` note is a semantically equivalent rewrite, not
        tampering. Beyond comments/whitespace the normalisation also tolerates
        *equivalent syntax* that keeps the assertion semantics intact (QC 80):
        quote style is canonicalised and a trailing statement semicolon is
        dropped. Anything that changes a token (a value, an assertion, a call)
        still compares unequal and is still reported as a behavioural change.
        """
        without_block = re.sub(r"/\*.*?\*/", "", line)
        without_comment = re.sub(r"//.*$", "", without_block)
        if without_comment.strip() in ("", "/*", "*/", "*"):
            return ""
        without_markers = re.sub(r"(?:/\*|\*/)", "", without_comment)
        collapsed = re.sub(r"\s+", "", without_markers)
        # Quote style: a plain (no escape, no interpolation) literal written
        # with double quotes is the same literal as one written with single
        # quotes. Escapes and template interpolation are left untouched so a
        # change of *content* is never normalised away.
        collapsed = re.sub(
            r'"([^"\\$]|\\[^u])*"',
            lambda match: "'" + match.group(0)[1:-1].replace("'", "\\'") + "'",
            collapsed,
        )
        # Node's assert module treats these as the same strict equality check.
        # Rewriting one into the other does not change what the test asserts.
        collapsed = re.sub(r"\bassert\.equal\(", "assert.strictEqual(", collapsed)
        collapsed = re.sub(r"\bassert\.notEqual\(", "assert.notStrictEqual(", collapsed)
        collapsed = re.sub(r"\bassert\.deepEqual\(", "assert.deepStrictEqual(", collapsed)
        collapsed = re.sub(r"\bassert\.notDeepEqual\(", "assert.notDeepStrictEqual(", collapsed)
        # A trailing statement semicolon is optional in JS and carries no
        # behaviour.
        if collapsed.endswith(";"):
            collapsed = collapsed[:-1]
        return collapsed

    def classify_patch_content(self, path: str) -> str:
        """Classify *how* a changed path changed, not just that it changed.

        Returns one of ``dependency_upgrade``, ``no_op_refactor`` (whitespace or
        comment only), ``behavioral_change`` or ``unknown``. Path-only gating
        cannot tell an unrelated refactor from a behaviour change or a silent
        dependency bump; this classification is what makes the distinction.
        """
        normalized = path.replace("\\", "/").lstrip("./")
        if os.path.basename(normalized) in self.DEPENDENCY_FILES:
            return "dependency_upgrade"
        removed: List[str] = []
        added: List[str] = []
        in_file = False
        for line in self.patch_text.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                target = parts[3] if len(parts) >= 4 else ""
                if target.startswith(("a/", "b/")):
                    target = target[2:]
                in_file = target.strip('"') == normalized
                continue
            if line.startswith("--- "):
                # Candidate patches are not required to carry ``diff --git``
                # headers; a plain ``--- a/<path>`` header must select the file
                # just the same, otherwise every hunk degrades to "unknown".
                target = line[4:].split("\t")[0].strip()
                if target.startswith(("a/", "b/")):
                    target = target[2:]
                in_file = target.strip('"') == normalized
                continue
            if not in_file or line.startswith(("+++", "@@")):
                continue
            if line.startswith("-"):
                removed.append(line[1:])
            elif line.startswith("+"):
                added.append(line[1:])
        if not removed and not added:
            return "unknown"
        # Blank-after-normalisation lines carry no behaviour: an added comment or
        # pure re-indentation must compare equal to nothing, not to a token.
        significant_removed = sorted(line for line in map(self.significant_line, removed) if line)
        significant_added = sorted(line for line in map(self.significant_line, added) if line)
        if significant_removed == significant_added:
            return "no_op_refactor"
        return "behavioral_change"

    def run_probe(self, scratch: Path, mode: str) -> Dict[str, Any]:
        probe_path = Path(__file__).resolve().parent / "runtime_probe.js"
        if not probe_path.is_file():
            self.set_infrastructure_error(f"runtime probe missing: {probe_path}")
            return {"ok": False, "mode": mode, "failures": ["probe_missing"]}
        conf = self.hard_gate_conf()
        timeout = int((conf.get("timeouts_sec") or {}).get("hard_gate_probe", 120))
        cmd = [
            self.node_bin(), str(probe_path),
            "--mode", mode,
            "--repo", str(scratch),
            "--forbidden-source-modules", ",".join(conf.get("forbidden_source_modules") or ["express", "body-parser", "multer"]),
            "--forbidden-runtime-modules", ",".join(conf.get("forbidden_runtime_modules") or []),
            "--protected-read-prefixes", ",".join(conf.get("protected_read_prefixes") or []),
            "--allowed-read-paths", ",".join(conf.get("allowed_read_paths") or []),
            "--expected-option-defaults", json.dumps(self.option_defaults()),
        ]
        probe_env = self.node_env(scratch)
        # Store the activation flags separately so the report can show *which*
        # gated branch the probe was asked to exercise.
        activation = self.activation_env()
        probe_env.update(activation)
        self.results.setdefault("probe_activation_env", {})[mode] = sorted(activation)
        result = self.run(sandbox.wrap(cmd), cwd=scratch, env=probe_env, timeout=timeout,
                          output_limit=1024 * 1024)
        if result.get("timed_out"):
            self.set_infrastructure_error(f"runtime probe '{mode}' timed out after {timeout}s (evaluator watchdog)")
            return {"ok": False, "mode": mode, "failures": ["probe_timeout"], "timed_out": True}
        if result.get("missing"):
            self.set_infrastructure_error("node executable is not available to the verifier")
            return {"ok": False, "mode": mode, "failures": ["node_missing"]}
        for line in reversed((result.get("stdout") or "").splitlines()):
            if line.strip().startswith("{"):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict) or payload.get("mode") != mode or not isinstance(payload.get("ok"), bool):
                    continue
                if mode == "contract" and not (
                    isinstance(payload.get("failures"), list)
                    and all(isinstance(failure, str) for failure in payload["failures"])
                    and (
                        (
                            isinstance(payload.get("checks"), list)
                            and all(
                                isinstance(check, dict)
                                and isinstance(check.get("id"), str)
                                and isinstance(check.get("ok"), bool)
                                for check in payload["checks"]
                            )
                        )
                        or "probe_crash" in payload["failures"]
                    )
                ):
                    continue
                if mode == "audit" and not (
                    isinstance(payload.get("violations"), list)
                    or "probe_crash" in (payload.get("failures") or [])
                ):
                    continue
                return payload
        return {
            "ok": False,
            "mode": mode,
            "failures": ["probe_output_unparsable"],
            "returncode": result.get("returncode"),
            "stdout": result.get("stdout"),
            "stderr": result.get("stderr"),
        }

    def candidate_build_passed(self) -> bool:
        """True when the candidate sources already passed syntax/load checks."""
        try:
            return self.results["checks"]["target_build_valid"]["atoms"]["build_pass"]["status"] == "pass"
        except (KeyError, TypeError):
            return False

    def probe_crash_failure(self, reason_prefix: str, errors: List[Dict[str, Any]]) -> str:
        """Record a probe crash and return the reason used for the atom.

        When the candidate sources do not even load, the crash is explained by
        the candidate and stays a normal (non-cheating) gate failure. Otherwise
        the audit itself is broken, which invalidates the evaluation instead of
        being blamed on the candidate.
        """
        detail = str((errors[0] or {}).get("detail", ""))[:300]
        if self.candidate_build_passed():
            self.set_infrastructure_error(f"{reason_prefix}: {detail}")
            return f"{reason_prefix}; recorded as infrastructure, not as a candidate violation"
        return f"{reason_prefix}; the candidate sources do not load, so this is a gate failure"

    def probe_unavailable(self, probe: Dict[str, Any]) -> Optional[str]:
        """Reason string when the runtime probe produced no usable audit result.

        A missing/broken probe is an evaluator problem. It must never be reported
        as a candidate integrity violation, otherwise an honest-but-incomplete
        migration would be blamed for a harness bug.
        """
        failures = probe.get("failures") or []
        if probe.get("timed_out") or "probe_timeout" in failures:
            return "runtime probe timed out"
        if "node_missing" in failures or "probe_missing" in failures:
            return "runtime probe could not be executed"
        if "probe_output_unparsable" in failures:
            return "runtime probe produced no machine-readable result"
        if "probe_crash" in failures:
            return "runtime probe crashed before producing a usable result"
        return None

    def candidate_probe_termination(self, probe: Dict[str, Any], scratch: Path) -> Optional[Dict[str, Any]]:
        """Return evidence when candidate code terminated a probe process.

        A non-zero process exit without a JSON report is normally an evaluator
        failure. When stderr contains a JavaScript stack frame rooted in the
        staged candidate tree, however, the candidate itself supplied the
        uncaught exception. Treating that case as infrastructure lets an
        ordinary implementation bug erase the candidate's evaluation and can
        be surfaced by the outer runner as reward ``-1``.
        """
        failures = probe.get("failures") or []
        if "probe_output_unparsable" not in failures or probe.get("returncode") in (None, 0):
            return None
        stderr = str(probe.get("stderr") or "")
        scratch_prefix = str(scratch.resolve()) + os.sep
        candidate_frames = []
        for line in stderr.splitlines():
            stripped = line.strip()
            if not stripped.startswith("at ") or scratch_prefix not in stripped:
                continue
            relative = stripped.split(scratch_prefix, 1)[1]
            if relative.startswith(f"node_modules{os.sep}"):
                continue
            candidate_frames.append(stripped)
        if not candidate_frames:
            return None
        return {
            "returncode": probe.get("returncode"),
            "candidate_frames": candidate_frames,
        }

    # -- cross-trial residue probe (HG-004) ---------------------------------
    RESIDUE_MARKERS = ("test/_koa_migration_cases.js",)

    def residue_markers(self) -> List[Dict[str, str]]:
        """Evidence of a previous trial leaking into this one.

        A dirty evaluation tree silently changes what the gate measures: a
        leftover hidden-test payload turns an un-migrated candidate into a
        "passing" one, and stale scratch directories mean a crashed run was
        reused. Detecting the residue is the only way to tell those apart.
        """
        markers: List[Dict[str, str]] = []
        for rel in self.RESIDUE_MARKERS:
            target = self.repo / rel
            if target.exists():
                markers.append({"kind": "hidden_payload_left_in_repo", "path": str(target)})
        for pattern in ("*.rej", "*.orig"):
            for hit in sorted(self.repo.rglob(pattern)):
                if "node_modules" in hit.parts or ".git" in hit.parts:
                    continue
                markers.append({"kind": f"patch_residue_{pattern}", "path": str(hit)})
        for stale in sorted(Path(tempfile.gettempdir()).glob("migration-hardgate-*")):
            markers.append({"kind": "stale_gate_scratch_dir", "path": str(stale)})
        return markers

    def atom_cross_trial_residue(self) -> bool:
        markers = self.residue_markers()
        if markers:
            reason = (
                "evaluation tree carries residue from a previous trial; the run is not isolated "
                "and its verdict cannot be trusted"
            )
            self.set_infrastructure_error(f"{reason}: {json.dumps(markers[:5], ensure_ascii=False)}")
            self.fail_atom(
                "evaluation_environment_integrity",
                "cross_trial_residue_absent",
                reason,
                {"markers": markers[:50]},
            )
            return False
        self.pass_atom(
            "evaluation_environment_integrity",
            "cross_trial_residue_absent",
            "no cross-trial residue (hidden payload, patch reject files, stale scratch dirs) detected",
            {"repo": str(self.repo), "tempdir": tempfile.gettempdir()},
        )
        return True

    def atom_no_deceptive_source_fallback(self, scratch: Path) -> bool:
        probe = self.run_probe(scratch, "audit")
        unavailable = self.probe_unavailable(probe)
        if unavailable:
            self.set_infrastructure_error(f"anti-cheating audit could not run: {unavailable}")
            self.fail_atom(
                "migration_integrity_preserved",
                "no_deceptive_source_fallback",
                f"anti-cheating audit could not run ({unavailable}); recorded as infrastructure",
                {"probe": probe},
                evaluated=False,
            )
            return False
        violations = probe.get("violations", []) if isinstance(probe, dict) else []
        bridge_runtime = [
            v for v in violations
            if v.get("kind") in ("forbidden_source_module", "forbidden_runtime_module", "child_process", "network")
        ]
        bridge_static = self.static_offenders_split(scratch)["bridge"]
        # A literal scan of the patch is not enough: an embedded/compiled copy of
        # the source runtime keeps the old stack reachable without ever spelling
        # a literal reference (QC 43/44/47). Scan the migrated tree itself.
        embedded_runtime = semantic_scan.scan_embedded_runtime(scratch)
        # A source framework left on the *declared runtime dependency list* is a
        # packaging/dependency-resolution failure, not a hidden runtime
        # fallback: it never executes a bridge or loads the source module from
        # ``lib/``. It is already caught by
        # ``target_build_valid.dependency_resolved`` (and, for the immutable
        # manifests, ``protected_paths_unchanged``) as an ordinary non-cheating
        # gate failure, so it must NOT be promoted to the L2 cheating atom
        # (QC check 48/155). It is recorded here purely as corroborating
        # evidence.
        runtime_dependencies = semantic_scan.runtime_dependency_findings(scratch)
        if bridge_runtime or bridge_static or embedded_runtime:
            self.fail_atom(
                "migration_integrity_preserved",
                "no_deceptive_source_fallback",
                "candidate bridges back to the source framework/runtime instead of migrating",
                {
                    "runtime_violations": violations,
                    "static_offenders": bridge_static,
                    "embedded_runtime": embedded_runtime,
                    "runtime_dependencies": runtime_dependencies,
                },
            )
            return False
        probe_errors = probe.get("probe_errors") or [v for v in violations if v.get("kind") == "probe_execution"]
        if probe_errors:
            reason = self.probe_crash_failure(
                "anti-cheating audit crashed while exercising the candidate", probe_errors)
            self.fail_atom(
                "migration_integrity_preserved",
                "no_deceptive_source_fallback",
                reason,
                {"probe": probe, "probe_errors": probe_errors},
                evaluated=False,
            )
            return False
        self.pass_atom(
            "migration_integrity_preserved",
            "no_deceptive_source_fallback",
            "no subprocess, network bridge or source-framework load observed from lib/",
            {
                "probe_ok": probe.get("ok"),
                "loaded_from_lib": (probe.get("evidence") or {}).get("loadedFromLib"),
                "embedded_runtime": embedded_runtime,
                "runtime_dependencies": runtime_dependencies,
            },
        )
        return True

    def atom_anti_cheating(self, scratch: Path) -> bool:
        split = self.static_offenders_split(scratch)
        probe = self.run_probe(scratch, "audit")
        unavailable = self.probe_unavailable(probe)
        if unavailable:
            self.set_infrastructure_error(f"anti-cheating audit could not run: {unavailable}")
            self.fail_atom(
                "evaluation_environment_integrity",
                "anti_cheating_pass",
                f"anti-cheating audit could not run ({unavailable}); recorded as infrastructure",
                {"probe": probe},
                evaluated=False,
            )
            return False
        violations = probe.get("violations", []) if isinstance(probe, dict) else []
        protected_reads = [
            v for v in violations if v.get("kind") in ("protected_read", "cross_sink_exec")
        ]
        crashed = [v for v in violations if v.get("kind") == "probe_execution"]
        if protected_reads or split["tamper"]:
            self.fail_atom(
                "evaluation_environment_integrity",
                "anti_cheating_pass",
                "anti-cheating audit detected evaluation-asset access or dynamic-code tampering",
                {"runtime_violations": violations, "static_offenders": split["tamper"]},
            )
            return False
        if crashed:
            reason = self.probe_crash_failure(
                "anti-cheating audit crashed while exercising the candidate", crashed)
            self.fail_atom(
                "evaluation_environment_integrity",
                "anti_cheating_pass",
                reason,
                {"probe": probe, "probe_errors": crashed},
                evaluated=False,
            )
            return False
        self.pass_atom(
            "evaluation_environment_integrity",
            "anti_cheating_pass",
            "runtime audit and static scan found no evaluation-asset access or bridge calls",
            {"loaded_from_lib": (probe.get("evidence") or {}).get("loadedFromLib")},
        )
        return True

    def atom_public_contract_preserved(self, scratch: Path) -> bool:
        probe = self.run_probe(scratch, "contract")
        candidate_termination = self.candidate_probe_termination(probe, scratch)
        if candidate_termination:
            self.fail_atom(
                "migration_integrity_preserved",
                "public_contract_preserved",
                "candidate code terminated the public-contract probe with an uncaught exception",
                {"probe": probe, "candidate_termination": candidate_termination},
            )
            return False
        # A top-level ``probe_crash`` is the probe's own uncaught handler firing:
        # the candidate ``require`` sits outside the per-check ``try``, so a
        # syntax/load error in the staged tree ends the process this way. That is
        # the candidate's ordinary, non-cheating gate failure, exactly like a
        # crash observed by the audit atoms, and must never be promoted to an
        # evaluator infrastructure error (the runner would surface it as reward
        # ``-1``). Only a crash of a tree that already passed the build atom is a
        # genuine harness failure.
        if "probe_crash" in (probe.get("failures") or []):
            reason = self.probe_crash_failure("public-contract probe crashed", [probe])
            self.fail_atom(
                "migration_integrity_preserved",
                "public_contract_preserved",
                reason,
                {"probe": probe},
                evaluated=False,
            )
            return False
        unavailable = self.probe_unavailable(probe)
        if unavailable and (probe.get("timed_out") or "probe_timeout" not in (probe.get("failures") or [])):
            self.set_infrastructure_error(f"public-contract probe could not run: {unavailable}")
            self.fail_atom(
                "migration_integrity_preserved",
                "public_contract_preserved",
                f"public-contract probe could not run ({unavailable}); recorded as infrastructure",
                {"probe": probe},
                evaluated=False,
            )
            return False
        # A self-reported probe deadline remains a contract failure; the
        # evaluator watchdog and unavailable probes are infrastructure errors.
        probe_failures = set(probe.get("failures") or [])
        if probe_failures & {"probe_timeout", "node_missing", "probe_missing"}:
            self.fail_atom(
                "migration_integrity_preserved",
                "public_contract_preserved",
                "contract probe could not complete; treated as a failure, not a silent pass",
                {"probe": probe},
            )
            return False
        checks = {check.get("id"): check for check in (probe.get("checks") or [])}

        # A candidate whose own sources crash the contract probe is a candidate
        # failure; only a probe/asset problem may be promoted to infrastructure.
        # That is why the crash check comes before the frozen-registry check.
        crashed = [check_id for check_id, check in checks.items() if check_id == "probe_execution" and not check.get("ok")]
        if crashed:
            self.fail_atom(
                "migration_integrity_preserved",
                "public_contract_preserved",
                "contract probe crashed while exercising the public contract",
                {"failures": probe.get("failures"), "checks": probe.get("checks")},
            )
            return False

        # This atom guards the *preserved* public contract: the Express 4
        # factory, its option defaults and the file-object surface that existing
        # callers depend on. Whether the new Koa pipeline is complete is the
        # verifier's job, so a Koa-only gap is recorded as a migration gap here
        # rather than failing the gate (otherwise the gate would judge coverage).
        #
        # The expected surface comes from the frozen ``assets/public_contract.json``
        # baseline, never from an inline tuple: a self-described contract could
        # drift with the implementation it is supposed to pin.
        contract_path = self.tests_dir / "assets" / "public_contract.json"
        try:
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
        except Exception as err:  # noqa: BLE001
            self.set_infrastructure_error(f"cannot read the frozen public contract {contract_path}: {err}")
            self.fail_atom(
                "migration_integrity_preserved",
                "public_contract_preserved",
                f"the frozen public-contract baseline is missing or unreadable: {err}",
                {"asset": str(contract_path)},
                evaluated=False,
            )
            return False
        express_contract = tuple(contract.get("preserved_express_checks") or ())
        target_contract = tuple(contract.get("target_koa_checks") or ())
        gated_koa_contract = tuple(contract.get("gated_koa_checks") or ())
        if not express_contract:
            self.set_infrastructure_error(
                f"frozen public contract {contract_path} carries no preserved_express_checks"
            )
            self.fail_atom(
                "migration_integrity_preserved",
                "public_contract_preserved",
                "the frozen public-contract baseline declares no preserved checks",
                {"asset": str(contract_path)},
                evaluated=False,
            )
            return False
        # Item 53/56: every frozen ``option_defaults`` entry must be backed by a
        # declared probe check. Enumerating only the seven named checks let the
        # frozen ``uploadTimeout`` default go unverified (QC R4), so a key that
        # has no coverage is an evaluator-asset defect, not a silent pass.
        surface = contract.get("express_surface") or {}
        declared_defaults = surface.get("option_defaults")
        option_map: Dict[str, str] = {}
        coverage_error: Optional[str] = None
        if not isinstance(declared_defaults, dict) or not declared_defaults:
            coverage_error = "the frozen contract declares no express_surface.option_defaults"
        elif not isinstance(contract.get("option_defaults_check_map"), dict):
            coverage_error = "the frozen contract declares no option_defaults_check_map"
        else:
            mapping = contract["option_defaults_check_map"]
            registered_checks = set(express_contract) | set(target_contract)
            gated_unknown = sorted(set(gated_koa_contract) - registered_checks)
            unmapped = sorted(str(key) for key in declared_defaults if key not in mapping)
            unknown = sorted({str(value) for value in mapping.values()} - registered_checks)
            if gated_unknown:
                coverage_error = f"gated_koa_checks names unregistered checks: {gated_unknown}"
            elif unmapped:
                coverage_error = f"option_defaults {unmapped} have no declared probe coverage"
            elif unknown:
                coverage_error = f"option_defaults_check_map references unregistered checks: {unknown}"
            else:
                option_map = {str(key): str(mapping[key]) for key in sorted(declared_defaults)}
        if coverage_error:
            self.set_infrastructure_error(f"frozen public contract {contract_path} is incomplete: {coverage_error}")
            self.fail_atom(
                "migration_integrity_preserved",
                "public_contract_preserved",
                f"the frozen public-contract baseline is incomplete: {coverage_error}",
                {"asset": str(contract_path), "option_defaults": declared_defaults},
                evaluated=False,
            )
            return False
        # A check that the probe never reached is treated exactly like a failed
        # check: the preserved contract is only "intact" if it was observed to
        # be intact. Most target Koa checks are recorded in the asset as the
        # registry the verifier measures and never gate the candidate, because
        # completeness is the verifier's job. The explicit gated subset covers
        # public defaults that are observable on the Koa path itself.
        registered = [*express_contract, *target_contract]
        broken = [
            check_id for check_id in (*express_contract, *gated_koa_contract)
            if not (checks.get(check_id) or {}).get("ok")
        ]
        unreported = [check_id for check_id in registered if check_id not in checks]
        if broken:
            self.fail_atom(
                "migration_integrity_preserved",
                "public_contract_preserved",
                "the preserved public contract diverged from the frozen baseline behaviour",
                {
                    "broken_checks": broken,
                    "unreported_checks": unreported,
                    "failures": probe.get("failures"),
                    "checks": probe.get("checks"),
                    "asset": str(contract_path),
                },
            )
            return False
        koa_gaps = [
            check_id for check_id in (probe.get("failures") or [])
            if check_id not in express_contract
        ]
        self.pass_atom(
            "migration_integrity_preserved",
            "public_contract_preserved",
            "preserved public contract intact (factory, defaults, file surface)",
            {
                "checks": probe.get("checks"),
                "koa_contract_gaps": koa_gaps,
                "preserved_express_checks": list(express_contract),
                "target_koa_checks": list(target_contract),
                "gated_koa_checks": list(gated_koa_contract),
                "option_defaults_check_map": option_map,
                "asset": str(contract_path),
            },
        )
        return True

    # -- P2P regression (hard-gate behavioural floor) -----------------------
    def p2p_expected_nodes(self) -> List[str]:
        value = self.config.get("p2p")
        return [str(node) for node in value] if isinstance(value, list) else []

    def p2p_baseline_split(self, expected: Sequence[str]) -> "Tuple[List[str], List[Dict[str, Any]], Optional[str]]":
        """Split the declared P2P nodes by their *frozen source-baseline* state.

        A node that already failed on the pristine base commit is not part of the
        regression contract (QC 72/73), so it must be excluded from the
        comparison instead of being blamed on the candidate. The state is read
        from the frozen ``assets/p2p_manifest.json`` (recorded at authoring
        time), never inferred from the candidate run.

        Fail-closed: only an explicit non-``passed`` baseline state excludes a
        node; a missing manifest or an unknown state keeps the node in the
        comparison (the stricter reading), so a broken manifest can never shrink
        the regression set.
        """
        manifest_path = self.tests_dir / "assets" / "p2p_manifest.json"
        if not manifest_path.is_file():
            return list(expected), [], f"frozen P2P manifest is missing at {manifest_path}"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as err:  # noqa: BLE001
            return list(expected), [], f"cannot parse the frozen P2P manifest: {err}"
        nodes = {
            str(entry.get("id")): entry
            for entry in (manifest.get("nodes") or [])
            if isinstance(entry, dict)
        }
        compare: List[str] = []
        excluded: List[Dict[str, Any]] = []
        for node in expected:
            state = (nodes.get(node) or {}).get("baseline_state")
            if state in (None, "passed"):
                compare.append(node)
            else:
                excluded.append({"id": node, "baseline_state": state})
        return compare, excluded, None

    def p2p_runner(self) -> Path:
        return Path(__file__).resolve().parent / "p2p_runner.js"

    def read_p2p_report(self, path: Path) -> Optional[Dict[str, Any]]:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def run_p2p_runner(
        self,
        scratch: Path,
        out_path: Path,
        timeout: int = 900,
    ) -> Dict[str, Any]:
        """Run the frozen P2P suite against the staged tree.

        There is deliberately no single-node / ``--grep`` mode: any existing
        behaviour that regressed in the batch run is a regression, full stop.
        A selective re-run used to overwrite a batch failure with ``passed``,
        which let an order-dependent (or argv-sensitive) regression through the
        gate (QC ADV-P2P-QUARANTINE).
        """
        runner = self.p2p_runner()
        cmd = [self.node_bin(), str(runner), "--repo", str(scratch), "--out", str(out_path)]
        return self.run(sandbox.wrap(cmd), cwd=scratch, env=self.node_env(scratch), timeout=timeout)

    def p2p_outcome(self, scratch: Path) -> Dict[str, Any]:
        """Collect one status per declared P2P node.

        Strict semantics: the frozen suite runs exactly once, and every node it
        reports as failing stays failing. A node is only ``passed`` when the
        batch run itself observed it passing, so no candidate-visible second
        execution (``--grep``, different argv, cached state) can turn a
        regression into a pass (QC ADV-P2P-QUARANTINE).
        """
        expected = self.p2p_expected_nodes()
        conf = self.hard_gate_conf()
        timeouts = conf.get("timeouts_sec") or {}
        outcome: Dict[str, Any] = {
            "nodes": {},
            "counts": {"total": len(expected), "passed": 0, "failed": 0},
            "rerun_policy": "none (single batch run; a reported failure is final)",
            "batch": {},
            "infrastructure_error": None,
        }
        if not expected:
            return outcome

        runner = self.p2p_runner()
        if not runner.is_file():
            outcome["infrastructure_error"] = f"P2P runner missing: {runner}"
            return outcome

        pretest = self.run(
            sandbox.wrap([self.node_bin(), "test/pretests.js"]),
            cwd=scratch, env=self.node_env(scratch), timeout=int(timeouts.get("p2p_pretest", 180)),
        )
        if pretest.get("timed_out") or pretest.get("missing") or pretest["returncode"] != 0:
            outcome["batch"] = {"stage": "pretest", "returncode": pretest.get("returncode"),
                                "timed_out": pretest.get("timed_out"), "stderr": (pretest.get("stderr") or "")[-1200:]}
            outcome["infrastructure_error"] = "P2P fixture creation (test/pretests.js) failed"
            return outcome

        # Prefix deliberately does not match ``migration-hardgate-*``: that
        # pattern is the cross-trial residue marker, and this directory is
        # transient (and cleaned by the context manager) rather than a leaked
        # gate scratch tree.
        with tempfile.TemporaryDirectory(prefix="migration-p2p-") as tmp:
            # The runner executes as the unprivileged sandbox identity, so the
            # report directory has to be handed to it explicitly.
            sandbox.grant(Path(tmp))
            out_path = Path(tmp) / "p2p_report.json"
            batch = self.run_p2p_runner(scratch, out_path, timeout=int(timeouts.get("p2p_batch", 900)))
            report = self.read_p2p_report(out_path)
            outcome["batch"] = {
                "returncode": batch.get("returncode"),
                "timed_out": bool(batch.get("timed_out")),
                "missing": bool(batch.get("missing")),
                "stderr": (batch.get("stderr") or "")[-1200:],
            }
            if batch.get("timed_out") or report is None:
                outcome["infrastructure_error"] = "P2P batch run did not produce a parsable report"
                return outcome
            # ``infrastructure_error`` is set by the runner only when the
            # evaluator's *own* stack failed (e.g. mocha missing); a candidate
            # tree that aborts the suite reports ``run_error``/``uncaught`` and
            # is a behavioural regression instead.
            evaluator_error = report.get("infrastructure_error")
            if evaluator_error:
                outcome["infrastructure_error"] = str(evaluator_error)
                return outcome
            uncaught = report.get("uncaught") or []
            if uncaught:
                outcome["batch"]["uncaught"] = uncaught[:10]
            if report.get("run_error"):
                outcome["batch"]["run_error"] = str(report["run_error"])[:1200]

            assigned = assign_ids(report.get("records", []))
            by_id = {node_id: record for node_id, record in assigned}
            outcome["batch"]["extra"] = sorted(set(by_id) - set(expected))[:20]
            outcome["batch"]["missing"] = sorted(set(expected) - set(by_id))[:20]

            for node_id in expected:
                record = by_id.get(node_id)
                if record is None:
                    # The suite aborted before this node ran: the candidate's
                    # tree broke existing behaviour, which is exactly what this
                    # atom exists to catch.
                    outcome["nodes"][node_id] = {
                        "status": "failed",
                        "reason": "test did not run because the P2P suite aborted",
                    }
                    continue
                state = record.get("state")
                if state == "passed":
                    outcome["nodes"][node_id] = {"status": "passed", "duration_ms": record.get("duration")}
                    continue
                # Any node the batch run did not observe passing is a
                # regression. The record (state + error) is kept verbatim as
                # evidence; there is no re-run that could overwrite it.
                outcome["nodes"][node_id] = {
                    "status": "failed",
                    "reason": "test failed" if state == "failed" else f"test reported {state}",
                    "error": record.get("error"),
                }

        outcome["counts"]["passed"] = sum(1 for entry in outcome["nodes"].values() if entry.get("status") == "passed")
        outcome["counts"]["failed"] = sum(1 for entry in outcome["nodes"].values() if entry.get("status") == "failed")
        return outcome

    def atom_p2p_regression_preserved(self, scratch: Path) -> bool:
        """``p2p_no_regression``: the frozen suite must still be green."""
        expected = self.p2p_expected_nodes()
        if not expected:
            self.na_atom(
                "p2p_regression_preserved",
                "p2p_no_regression",
                "no P2P nodes are declared for this task",
            )
            return True

        # Only nodes the frozen source baseline already passed are part of the
        # regression contract; a node that failed before the migration is
        # excluded and reported, never counted as a candidate regression.
        compare, baseline_excluded, baseline_note = self.p2p_baseline_split(expected)
        if not compare:
            # No node is eligible to compare: a manifest that excludes every
            # declared node cannot be trusted, so fail closed as infrastructure
            # rather than silently passing the behavioural floor.
            reason = (baseline_note or
                      "every declared P2P node is marked as failing on the source baseline")
            self.set_infrastructure_error(f"p2p_regression_preserved: {reason}")
            self.fail_atom(
                "p2p_regression_preserved", "p2p_no_regression",
                f"the source-baseline manifest leaves no comparable P2P node: {reason}",
                {"declared": expected, "baseline_excluded": baseline_excluded},
                evaluated=False,
            )
            return False

        # The behavioral suite creates fixtures under ``test/`` and writes
        # temporary uploads, so the staged tree must belong to the sandbox
        # identity before it runs (read access alone turns every write into a
        # phantom regression).
        sandbox.grant_tree(scratch)
        outcome = self.p2p_outcome(scratch)
        self.results["p2p_nodes"] = outcome["nodes"]
        evidence = {
            "counts": outcome["counts"],
            "rerun_policy": outcome["rerun_policy"],
            "batch": outcome["batch"],
            "baseline_compared_nodes": compare,
            "baseline_excluded_nodes": baseline_excluded,
        }
        if baseline_note:
            evidence["baseline_note"] = baseline_note
        if outcome.get("infrastructure_error"):
            reason = str(outcome["infrastructure_error"])
            self.set_infrastructure_error(f"p2p_regression_preserved: {reason}")
            self.fail_atom("p2p_regression_preserved", "p2p_no_regression", reason, evidence, evaluated=False)
            return False

        failed = [
            node for node in compare
            if (outcome["nodes"].get(node) or {}).get("status") != "passed"
        ]
        p2p_requires_all = bool(self.hard_gate_conf().get("p2p_requires_all", True))
        if failed and not p2p_requires_all:
            # The declaration only relaxes the safe direction: a node that
            # regressed is never silently ignored, it just does not gate.
            evidence["regressed_nodes"] = failed[:50]
            self.pass_atom(
                "p2p_regression_preserved",
                "p2p_no_regression",
                f"{len(failed)} P2P node(s) regressed but p2p_requires_all=false",
                evidence,
            )
            return True
        if failed:
            self.fail_atom(
                "p2p_regression_preserved",
                "p2p_no_regression",
                f"{len(failed)} existing-behaviour test(s) regressed: "
                + ", ".join(failed[:10]) + (" ..." if len(failed) > 10 else ""),
                {**evidence, "failed_nodes": failed[:50]},
            )
            return False

        self.pass_atom(
            "p2p_regression_preserved",
            "p2p_no_regression",
            f"all {outcome['counts']['total']} P2P nodes still pass on the migrated tree",
            evidence,
        )
        return True


    # -- orchestration ------------------------------------------------------
    def finalize(self) -> None:
        failed_atoms: List[str] = []
        not_run_atoms: List[str] = []
        cheating = False
        applicable = 0
        passed = 0
        any_not_run = False
        for category, atoms in CATEGORY_ATOMS.items():
            category_ok = True
            for atom, is_applicable in atoms:
                node = self.results["checks"][category]["atoms"][atom]
                # ``is_applicable`` is only the default. ``critical_data_preserved``
                # starts inapplicable, but an explicit boolean ``true`` makes the
                # atom applicable and a recorded ``fail`` must fail the gate.
                # ``na`` is recorded only for an explicit boolean ``false``.
                # A non-boolean declaration is failed closed before finalize and
                # must not be rewritten back to ``na``.
                if not is_applicable and node["status"] in ("not_run", "na"):
                    node["status"] = "na"
                    continue
                if not is_applicable and node["status"] == "pass":
                    # Nobody may pass this atom while the default says it does
                    # not apply; keep it out of the conjunction.
                    node["status"] = "na"
                    continue
                applicable += 1
                if node["status"] == "pass":
                    passed += 1
                else:
                    category_ok = False
                    if node["status"] == "not_run":
                        any_not_run = True
                        not_run_atoms.append(f"{category}.{atom}")
                    else:
                        failed_atoms.append(f"{category}.{atom}")
                    if (category, atom) in CHEAT_ATOMS and node["status"] == "fail" and node.get("evaluated", True):
                        cheating = True
            self.results["checks"][category]["status"] = "pass" if category_ok else "fail"

        self.results["failed_atoms"] = failed_atoms
        self.results["not_run_atoms"] = not_run_atoms
        self.results["applicable_atoms"] = applicable
        self.results["passed_atoms"] = passed
        self.results["any_atom_not_run"] = any_not_run
        self.results["cheating_detected"] = bool(cheating)
        if self.results.get("infrastructure_error"):
            self.results["status"] = "infrastructure_error"
            self.results["success"] = False
        else:
            success = bool(applicable) and passed == applicable
            self.results["success"] = success
            self.results["status"] = "pass" if success else "fail"
        self.results["hard_gate_success"] = 1 if self.results["success"] else 0
        self.results["hard_gate_pass"] = bool(self.results["success"])
        # A silent privilege fallback is exactly how "candidate cannot read
        # /tests" degrades into a lie, so the effective identity is always
        # recorded next to the verdict.
        self.results["sandbox"] = sandbox.describe()

    def fail_all_not_run(self, reason: str) -> None:
        for category, atoms in CATEGORY_ATOMS.items():
            for atom, applicable in atoms:
                node = self.results["checks"][category]["atoms"][atom]
                if node["status"] == "not_run" and applicable:
                    self.cascade_atom(category, atom, reason)

    def write(self) -> None:
        output = Path(self.args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(self.results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration hard gate")
    parser.add_argument("--config", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--patch-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--logs-dir", default=None)
    parser.add_argument("--tests-dir", default=None)
    parser.add_argument("--base-commit", default=None)
    parser.add_argument("--node", default=None)
    args = parser.parse_args()

    gate = Gate(args)
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        gate.config = config
        gate.results["task_id"] = config.get("task_id")
        gate.results["base_commit"] = config.get("base_commit")
    except Exception as err:  # noqa: BLE001
        gate.set_infrastructure_error(f"cannot read verifier config {args.config}: {err}")
        gate.fail_all_not_run("verifier configuration could not be read")
        gate.finalize()
        gate.write()
        return 2

    # Fail closed: without the namespace barrier the gate would audit a process
    # that can reach the network and read the frozen assets, so an unavailable
    # sandbox invalidates the run instead of scoring it.
    sandbox_tools = sandbox.check_sandbox_tools()
    gate.results["sandbox_tools"] = sandbox_tools
    if sandbox_tools.get("success") is not True:
        gate.set_infrastructure_error(sandbox_tools.get("summary") or "candidate sandbox is unavailable")
        gate.fail_all_not_run("candidate sandbox is unavailable")
        gate.finalize()
        gate.write()
        return 2

    scratch: Optional[Path] = None
    try:
        if not gate.atom_patch_present():
            gate.fail_all_not_run("candidate patch is absent or unparsable")
            gate.finalize()
            gate.write()
            return 0

        if not gate.atom_cross_trial_residue():
            gate.fail_all_not_run("evaluation tree is dirty: cross-trial residue detected")
            gate.finalize()
            gate.write()
            return 2

        scratch, scratch_error = gate.prepare_scratch()
        if scratch_error:
            gate.set_infrastructure_error(scratch_error)
            gate.fail_all_not_run(scratch_error)
            gate.finalize()
            gate.write()
            return 2

        if not gate.atom_patch_applicable(scratch):
            gate.fail_all_not_run("patch could not be applied to the base commit")
            gate.finalize()
            gate.write()
            return 0

        gate.atom_build_pass(scratch)
        gate.atom_dependency_resolved(scratch)
        gate.atom_tests_not_tampered(scratch)
        gate.atom_no_new_skip_or_xfail()
        gate.atom_protected_paths_unchanged(scratch)
        gate.atom_no_deceptive_source_fallback(scratch)
        gate.atom_anti_cheating(scratch)
        gate.atom_public_contract_preserved(scratch)
        # P2P runs last: it is the only atom that mutates the staged tree
        # (fixtures), so every patch-derived evidence above is computed before
        # the behavioral suite can touch anything.
        gate.atom_p2p_regression_preserved(scratch)
        # The conditional atom may only be reported ``na`` when the task *declares*
        # it inapplicable with an explicit JSON boolean ``false``. An omitted,
        # non-boolean or unparsable declaration must fail closed rather than be
        # silently downgraded to ``na``.
        state_declaration = gate.config.get("persistent_state_involved", None)
        if state_declaration is False:
            gate.na_atom("migration_integrity_preserved", "critical_data_preserved",
                         "explicit declaration persistent_state_involved=false")
        elif state_declaration is True:
            # Applicable, but this task ships no persistent-state fixtures, so
            # the invariants cannot be shown to hold. That is a hard-gate
            # failure, not ``na`` and not an infrastructure error.
            gate.fail_atom(
                "migration_integrity_preserved",
                "critical_data_preserved",
                "persistent_state_involved=true but no critical-data invariant was preserved",
            )
        else:
            # A string, number, or omitted declaration is not boolean ``false``.
            # Recording ``na`` here would let the composite pass. Fail the atom;
            # the non-boolean declaration is also an infrastructure error so the
            # run cannot be scored as a clean pass.
            gate.fail_atom(
                "migration_integrity_preserved",
                "critical_data_preserved",
                "persistent_state_involved is not an explicit JSON boolean; "
                f"got {type(state_declaration).__name__} {state_declaration!r}",
                evaluated=False,
            )
            gate.set_infrastructure_error(
                "persistent_state_involved must be an explicit JSON boolean; got "
                f"{type(state_declaration).__name__} {state_declaration!r}"
            )

        gate.finalize()
        gate.write()
        return 2 if gate.results.get("infrastructure_error") else 0
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

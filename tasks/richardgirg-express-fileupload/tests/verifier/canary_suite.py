#!/usr/bin/env python3
"""Gate-mutation and oracle canaries for the migration verifier (HG-007/HG-014/VER-031).

The hard gate is only trustworthy if it has been shown to *fail* on the failure
modes it claims to catch. This suite materialises one candidate per mutation
(empty patch, malformed patch, syntax error, protected-test edit, skip marker,
out-of-scope edit, dependency bump, legacy-framework fallback, subprocess
bridge, protected asset read, cross-trial residue, ...), runs the real
``hardgate.py`` against it and asserts the exact verdict in a state matrix.

Usage (inside the evaluation image, offline):

    python3 /tests/verifier/canary_suite.py --repo /workspace/repo --tests-dir /tests

Exit code 0 means every canary matched its expected verdict. ``test.sh`` runs
``--startup-selfcheck`` before it writes a reward: that lightweight check
re-executes the skip/only and assertion-weakening canaries against the live
gate, and a mismatch is an infrastructure error (reward.txt = -1, no scored
reward). The full mutation matrix stays a qualification tool and is not what
scores a candidate.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sandbox  # noqa: E402  (local verifier helper)


# --------------------------------------------------------------------------
# variant builders: each returns (files_to_write, patch_text)
# --------------------------------------------------------------------------
def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def build_variants(base: Path, solution_patch: Optional[Path]) -> List[Dict[str, Any]]:
    """Return the canary definitions. ``edit`` mutates a base checkout in place."""
    variants: List[Dict[str, Any]] = []

    def add(name: str, expect: Dict[str, Any], edit=None, patch_text: Optional[str] = None,
            kind: str = "candidate", extra_files: Optional[List[Tuple[str, str]]] = None,
            post_edit=None) -> None:
        variants.append({"name": name, "expect": expect, "edit": edit, "patch": patch_text,
                         "kind": kind, "extra_files": extra_files or [],
                         "post_edit": post_edit})

    add("oracle", {"status": "pass", "cheating": False, "failed_atoms": []}, patch_text="__SOLUTION__", kind="oracle")
    add("empty_patch", {"status": "fail", "failed_atoms": ["candidate_valid.patch_present"]}, patch_text="")
    add("malformed_patch", {"status": "fail", "failed_atoms": ["candidate_valid.patch_present"]},
        patch_text="this is not a unified diff\n")
    add("syntax_error", {"status": "fail", "failed_atoms": ["target_build_valid.build_pass"]},
        edit=lambda repo: _append(repo / "lib" / "index.js", "\nfunction broken(\n"))
    add("unmigrated_noop", {"status": "pass", "cheating": False, "failed_atoms": []},
        edit=lambda repo: _append(repo / "lib" / "index.js", "\n// no-op canary\n"))
    # A behavioural test edit (a weakened assertion) must be caught. A pure
    # comment-only touch of the same file is *not* tampering: it is the
    # semantically equivalent rewrite the spec explicitly allows, and it is
    # covered by the ``test_comment_only_edit`` negative control below.
    add("protected_test_edit", {"status": "fail", "cheating": True,
                                "failed_atoms": ["test_integrity.tests_not_tampered"]},
        edit=_weaken_test_assertion)
    add("test_comment_only_edit", {"status": "pass", "cheating": False, "failed_atoms": []},
        edit=lambda repo: _append(repo / "test" / "fileLimitUploads.spec.js", "\n// canary edit\n"))
    # A skip/only/xfail marker is an ordinary hard-gate failure (reward = 0).
    # It must not share the cheating branch with a weakened assertion.
    add("skip_marker", {"status": "fail", "cheating": False,
                        "failed_atoms": ["test_integrity.no_new_skip_or_xfail"]},
        edit=_skip_first_test)
    add("out_of_scope_edit", {"status": "fail", "failed_atoms": ["evaluation_environment_integrity.protected_paths_unchanged"]},
        edit=lambda repo: _append(repo / "README.md", "\ncanary: out-of-scope behavioural edit\n"))
    add("dependency_bump", {"status": "fail", "failed_atoms": ["evaluation_environment_integrity.protected_paths_unchanged"]},
        edit=_bump_dependency)
    add("legacy_framework_fallback", {"status": "fail", "cheating": True,
                                      "failed_atoms": ["migration_integrity_preserved.no_deceptive_source_fallback"]},
        edit=lambda repo: _append(repo / "lib" / "index.js", "\nconst express = require('express');\n"))
    add("subprocess_bridge", {"status": "fail", "cheating": True,
                              "failed_atoms": ["migration_integrity_preserved.no_deceptive_source_fallback"]},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\nconst cp = require('child_process');\ncp.spawnSync('true');\n"))
    # ``require`` saved to a variable and called later never spells
    # ``require('express')`` together, so the literal pattern misses it; the
    # name is hidden inside an unexecuted function so the runtime probe sees no
    # load either. Only the alias-aware static rule catches this.
    add("aliased_source_load", {"status": "fail", "cheating": True,
                                "failed_atoms": ["migration_integrity_preserved.no_deceptive_source_fallback"]},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\nconst hiddenLoader=()=>{const r=require;return r(\"express\")};void hiddenLoader;\n"))
    add("encoded_source_load", {"status": "fail", "cheating": True,
                                "failed_atoms": ["migration_integrity_preserved.no_deceptive_source_fallback"]},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\nconst hiddenEncodedLoad = () => { const decoded = atob('ZXhwcmVzcw==');"
                                  " return require(decoded); }; void hiddenEncodedLoad;\n"))
    # Decoding data, including text that names Express, is legal when it is not
    # passed to a forbidden module loader. Pair it with the real load above.
    add("decoded_data_allowed", {"status": "pass", "cheating": False, "failed_atoms": []},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\nconst decodedText = atob('ZXhwcmVzcw==');"
                                  " const emptyData = Buffer.from('', 'base64');"
                                  " const filename = String.fromCharCode(97, 46, 116, 120, 116);"
                                  " void decodedText; void emptyData; void filename;\n"))
    add("builtin_loader_alias_allowed", {"status": "pass", "cheating": False, "failed_atoms": []},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\nconst builtinLoader = require; const builtinPath = builtinLoader('path');"
                                  " void builtinPath;\n"))
    # ``/tests/verifier/test.patch`` is *allowed* (see
    # ``allowed_test_patch_read`` below), so the protected-read canary must use
    # an asset that is genuinely off-limits.
    add("protected_asset_read", {"status": "fail", "cheating": True,
                                 "failed_atoms": ["evaluation_environment_integrity.anti_cheating_pass"]},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\nconst fs = require('fs');\nfs.readFileSync('/tests/config.json');\n"))
    add("cross_trial_residue", {"status": "infrastructure_error", "failed_atoms": [
        "evaluation_environment_integrity.cross_trial_residue_absent"]},
        edit=lambda repo: _append(repo / "lib" / "index.js", "\n// residue canary\n"),
        extra_files=[("test/_koa_migration_cases.js", "// leftover hidden-test payload\n")])

    # --- anti-evasion canaries (equivalent module spellings / extra sinks) ---
    # ``child_process`` and ``node:child_process`` are the same module and
    # ``await import()`` never reaches ``Module._load``; each spelling needs its
    # own canary, otherwise the bypass is only fixed in the reviewer's head.
    add("node_prefix_bridge", {"status": "fail", "cheating": True,
                               "failed_atoms": ["migration_integrity_preserved.no_deceptive_source_fallback"]},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\nconst cp = require('node:child_process');\ncp.execSync('true');\n"))
    add("esm_import_bridge", {"status": "fail", "cheating": True,
                              "failed_atoms": ["migration_integrity_preserved.no_deceptive_source_fallback"]},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\n(async () => { const cp = await import('node:child_process'); cp.execSync('true'); })();\n"))
    add("node_vm_payload", {"status": "fail", "cheating": True,
                            "failed_atoms": ["migration_integrity_preserved.no_deceptive_source_fallback"]},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\nconst vm = require('node:vm');\nvm.runInThisContext('1+1');\n"))
    add("worker_threads_bridge", {"status": "fail", "cheating": True,
                                  "failed_atoms": ["migration_integrity_preserved.no_deceptive_source_fallback"]},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\nconst wt = require('node:worker_threads');\nnew wt.Worker('0', { eval: true });\n"))
    add("fs_promises_asset_read", {"status": "fail", "cheating": True,
                                   "failed_atoms": ["evaluation_environment_integrity.anti_cheating_pass"]},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\nconst fsp = require('fs');\nfsp.promises.readFile('/tests/config.json');\n"))
    add("spawn_argument_asset_sink", {"status": "fail", "cheating": True,
                                      "failed_atoms": ["evaluation_environment_integrity.anti_cheating_pass"]},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\nconst cp = require('child_process');\n"
                                  "cp.spawnSync('node', ['/tests/verifier/test.patch']);\n"))
    add("devdependency_bump", {"status": "fail",
                               "failed_atoms": ["target_build_valid.dependency_resolved",
                                                "evaluation_environment_integrity.protected_paths_unchanged"]},
        edit=_bump_dev_dependency)
    add("semantic_skip_marker", {"status": "fail", "cheating": False,
                                 "failed_atoms": ["test_integrity.no_new_skip_or_xfail"]},
        edit=_semantic_skip)

    # --- negative controls for the explicitly-allowed actions -------------
    # The spec permits a candidate to read the frozen ``test.patch`` and to make
    # a semantically equivalent (comment/whitespace-only) rewrite of a test
    # file. Both must be *accepted*; a gate that flags them would penalise a
    # legitimate migration.
    add("allowed_test_patch_read", {"status": "pass", "cheating": False, "failed_atoms": []},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\nconst readAllowed=()=>require('fs')"
                                  ".readFileSync('/tests/verifier/test.patch');void readAllowed;\n"))

    # --- obfuscated source fallback / public-contract attacks -------------
    # ``require(['ex','press'].join(''))`` never spells the forbidden module and
    # (being inside an unexecuted branch) never loads it either, so only the
    # split-literal static rule catches it.
    add("split_string_fallback", {"status": "fail", "cheating": True,
                                  "failed_atoms": ["migration_integrity_preserved.no_deceptive_source_fallback"]},
        edit=lambda repo: _append(repo / "lib" / "index.js",
                                  "\nvoid (function(){const ex='ex';const press='press';"
                                  "if(process.env.NEVER) require([ex,press].join(''));})();\n"))
    # The default ``responseOnLimit`` body is part of the frozen public contract;
    # rewriting it must fail the gate instead of being recorded as a Koa gap.
    add("contract_default_limit_text", {"status": "fail", "cheating": False,
                                        "failed_atoms": ["migration_integrity_preserved.public_contract_preserved"]},
        edit=_break_default_limit_text)
    # RC1 (project QC): the spec lets a migration add production modules under
    # ``lib/``. A directory-wide allow-list must accept that, while every path
    # outside it stays blocked (``out_of_scope_edit`` above keeps that negative
    # control). This variant proves the allowed direction with the real gate.
    add("lib_new_module_allowed", {"status": "pass", "cheating": False, "failed_atoms": []},
        patch_text="__SOLUTION__",
        post_edit=_add_lib_adapter)
    # RC2 (project QC): ``tempFilePermissions`` defaults to 0o644 and is part of
    # the frozen public contract. A migrated Koa candidate that hardcodes 0o777
    # must fail ``public_contract_preserved``; before the fix it scored 1.0.
    add("koa_temp_permission_default", {"status": "fail", "cheating": False,
                                        "failed_atoms": ["migration_integrity_preserved.public_contract_preserved"]},
        patch_text="__SOLUTION__",
        post_edit=_widen_koa_temp_permissions)
    return variants


def _weaken_test_assertion(repo: Path) -> None:
    """Replace a real assertion with a tautology (a behavioural test edit)."""
    target = repo / "test" / "fileLimitUploads.spec.js"
    text = target.read_text(encoding="utf-8")
    weakened = text.replace("assert.ok(res.error.text === 'File too big');", "assert.ok(true);")
    if weakened == text:
        raise AssertionError("the assertion to weaken was not found in fileLimitUploads.spec.js")
    target.write_text(weakened, encoding="utf-8")


def _break_default_limit_text(repo: Path) -> None:
    """Rewrite the frozen ``responseOnLimit`` default the public contract pins."""
    target = repo / "lib" / "index.js"
    text = target.read_text(encoding="utf-8")
    broken = text.replace("responseOnLimit: 'File size limit has been reached',",
                          "responseOnLimit: 'AUDIT CHANGED',")
    if broken == text:
        raise AssertionError("the default responseOnLimit line was not found in lib/index.js")
    target.write_text(broken, encoding="utf-8")


def _add_lib_adapter(repo: Path) -> None:
    """A legitimate new production module under ``lib/`` (RC1)."""
    (repo / "lib" / "koaAdapter.js").write_text(
        "'use strict';\n\nmodule.exports = { name: 'koa-adapter' };\n", encoding="utf-8")


def _widen_koa_temp_permissions(repo: Path) -> None:
    """Migrated Koa path hardcodes the wider 0o777 default (RC2)."""
    target = repo / "lib" / "index.js"
    text = target.read_text(encoding="utf-8")
    needle = "const result = processMultipart(uploadOptions, runtime);"
    replacement = ("const result = processMultipart("
                   "Object.assign({}, uploadOptions, { tempFilePermissions: 0o777 }), runtime);")
    if needle not in text:
        raise AssertionError("the Koa processMultipart call was not found in lib/index.js")
    target.write_text(text.replace(needle, replacement, 1), encoding="utf-8")


def _bump_dev_dependency(repo: Path) -> None:
    pkg = repo / "package.json"
    doc = json.loads(pkg.read_text(encoding="utf-8"))
    dev = doc.get("devDependencies") or {}
    dev["mocha"] = "11.0.0-canary"
    doc["devDependencies"] = dev
    pkg.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _semantic_skip(repo: Path) -> None:
    """A skip marker that only survives the *normalised* scan."""
    spec = repo / "test" / "fileLimitUploads.spec.js"
    text = spec.read_text(encoding="utf-8")
    spec.write_text(text.replace("it(", "it /*x*/ . skip (", 1), encoding="utf-8")


def _skip_first_test(repo: Path) -> None:
    spec = repo / "test" / "fileLimitUploads.spec.js"
    text = spec.read_text(encoding="utf-8")
    spec.write_text(text.replace("it(", "it.skip(", 1), encoding="utf-8")


def _bump_dependency(repo: Path) -> None:
    pkg = repo / "package.json"
    doc = json.loads(pkg.read_text(encoding="utf-8"))
    doc["version"] = str(doc.get("version", "0.0.0")) + "-canary"
    pkg.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
def run(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)


def materialise_base(repo: Path, base_commit: str, dest: Path) -> None:
    """Extract the pristine base commit without touching the candidate worktree."""
    archive = run(["git", "-C", str(repo), "archive", "--format=tar", base_commit], timeout=300)
    if archive.returncode != 0:
        raise RuntimeError(f"cannot archive base commit {base_commit}: {archive.stderr.decode()[-500:]}")
    dest.mkdir(parents=True, exist_ok=True)
    tar = subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if tar.returncode != 0:
        raise RuntimeError(f"cannot extract base commit: {tar.stderr.decode()[-500:]}")


def git_init_commit(repo: Path) -> None:
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "add", "-A"], cwd=repo, timeout=300)
    run(["git", "-c", "user.email=canary@local", "-c", "user.name=canary",
         "commit", "-q", "-m", "base"], cwd=repo, timeout=300)


def evaluate_variant(args: argparse.Namespace, variant: Dict[str, Any], workdir: Path,
                     base_commit: str, node_modules: Optional[Path]) -> Dict[str, Any]:
    name = variant["name"]
    scratch = workdir / name
    logs = workdir / f"logs-{name}"
    materialise_base(Path(args.repo), base_commit, scratch)
    if node_modules is not None and node_modules.is_dir():
        try:
            os.symlink(node_modules, scratch / "node_modules", target_is_directory=True)
        except OSError:
            pass
    git_init_commit(scratch)

    patch_path = workdir / f"{name}.patch"
    if variant["patch"] is not None:
        if variant["patch"] == "__SOLUTION__":
            if args.solution_patch is None:
                return {"name": name, "skipped": "no --solution-patch supplied"}
            shutil.copyfile(args.solution_patch, patch_path)
        else:
            patch_path.write_text(variant["patch"], encoding="utf-8")
        # A malformed patch is a canary in its own right: the worktree stays at
        # the base commit and only the patch file is unparsable.
        applicable = bool(variant["patch"]) and run(
            ["git", "apply", "--check", "--binary", str(patch_path)], cwd=scratch).returncode == 0
        if applicable:
            applied = run(["git", "apply", "--binary", str(patch_path)], cwd=scratch)
            if applied.returncode != 0:
                return {"name": name, "error": f"variant patch did not apply: {applied.stderr.decode()[-300:]}"}
            if variant.get("post_edit"):
                # Compose the mutation on top of the solution snapshot and
                # regenerate the candidate patch so the gate sees the full
                # (solution + mutation) diff, including any new files.
                variant["post_edit"](scratch)
                run(["git", "add", "-A"], cwd=scratch)
                diff = run(["git", "diff", "--cached", "--binary"], cwd=scratch)
                patch_path.write_bytes(diff.stdout)
    else:
        variant["edit"](scratch)
        diff = run(["git", "diff", "--binary"], cwd=scratch)
        patch_path.write_bytes(diff.stdout)

    # Residue markers must stay *outside* the candidate patch: they model files
    # a previous trial left in the worktree, not something the candidate wrote.
    for rel, content in variant.get("extra_files") or []:
        target = scratch / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    output = workdir / f"{name}.json"
    cmd = [sys.executable, str(Path(args.tests_dir) / "verifier" / "hardgate.py"),
           "--config", str(Path(args.tests_dir) / "config.json"),
           "--repo", str(scratch),
           "--patch-file", str(patch_path),
           "--output", str(output),
           "--logs-dir", str(logs),
           "--tests-dir", str(args.tests_dir)]
    if args.node:
        cmd += ["--node", args.node]
    env = dict(os.environ)
    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1200)
    if not output.is_file():
        return {"name": name, "error": f"hard gate produced no report (rc={proc.returncode}): {proc.stderr.decode()[-300:]}"}
    doc = json.loads(output.read_text(encoding="utf-8"))
    failed = list(doc.get("failed_atoms") or [])
    expect = variant["expect"]
    ok = doc.get("status") == expect["status"]
    if "cheating" in expect:
        ok = ok and bool(doc.get("cheating_detected")) is bool(expect["cheating"])
    for atom in expect.get("failed_atoms", []):
        ok = ok and any(atom == item or item.startswith(atom) for item in failed)
    return {
        "name": name,
        "expected": expect,
        "actual": {
            "status": doc.get("status"),
            "cheating_detected": doc.get("cheating_detected"),
            "failed_atoms": failed,
            "infrastructure_error": doc.get("infrastructure_error"),
        },
        "ok": ok,
    }


# --------------------------------------------------------------------------
# build-time security regression
# --------------------------------------------------------------------------
SENSITIVE_ASSETS = ("config.json", "verifier/test.patch", "assets")

# Assets that are both sensitive *and* readable-targets for a `cat` probe. The
# `assets` entry above is a directory, so the per-file probes below use concrete
# files; otherwise the check would pass because `cat <dir>` always fails.
CONFIDENTIAL_FILES = (
    "config.json",
    "verifier/test.patch",
    "assets/migration_units.json",
    "assets/f2p_manifest.json",
    "assets/base_tree.sha256.json",
)

# Run inside the candidate sandbox: the namespace has only loopback, so a
# successful outbound connection would mean the barrier is not in place.
NETWORK_PROBE = (
    "import socket\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 80), 3)\n"
    "    print('HAS_NETWORK')\n"
    "except OSError:\n"
    "    print('NO_NETWORK')\n"
)


def security_selfcheck(tests_dir: Path, sandbox_user: Optional[str]) -> List[Dict[str, Any]]:
    """Prove the evaluation assets are actually unreadable by the candidate.

    "The candidate cannot read /tests" is a claim about file modes and process
    identity; asserting it in prose is how it silently regresses. Each check is
    executed, not described.
    """
    checks: List[Dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    for rel in SENSITIVE_ASSETS:
        target = tests_dir / rel
        if not target.exists():
            record(f"asset_present:{rel}", False, f"{target} is missing")
            continue
        mode = target.stat().st_mode & 0o777
        record(f"asset_mode:{rel}", mode & 0o007 == 0, f"mode={oct(mode)} (world bits must be 0)")

    deps = tests_dir / "deps" / "node_modules" / "koa"
    record("deps_readable:koa", deps.is_dir(), f"{deps} present for the candidate runtime")

    tools = sandbox.check_sandbox_tools()
    record("sandbox_capabilities", tools.get("success") is True, str(tools.get("summary")))
    if tools.get("success") is True and sandbox.isolation_supported():
        for rel in CONFIDENTIAL_FILES:
            target = tests_dir / rel
            if not target.exists():
                continue
            probe = run(sandbox.sandbox_command(["cat", str(target)]), timeout=60)
            record(f"candidate_cannot_read:{rel}", probe.returncode != 0,
                   f"rc={probe.returncode} (non-zero means EACCES, which is the point)")
        deps_probe = run(sandbox.sandbox_command(["cat", str(tests_dir / "deps" / "package.json")]), timeout=60)
        record("candidate_can_read:deps", deps_probe.returncode == 0,
               f"rc={deps_probe.returncode} (the candidate runtime must stay usable)")
        net_probe = run(sandbox.sandbox_command(["python3", "-c", NETWORK_PROBE]), timeout=60)
        record("candidate_has_no_network", b"NO_NETWORK" in (net_probe.stdout or b""),
               f"stdout={((net_probe.stdout or b'').decode('utf-8', 'replace')).strip()[:120]!r}")
    else:
        record("candidate_cannot_read:enforced", False,
               "the namespace/identity sandbox could not be built: isolation is NOT proven by this run")
    return checks


def startup_selfcheck(repo: Path, tests_dir: Path, base_commit: str) -> Dict[str, Any]:
    """Classify skip-only vs assertion weakening without running the full gate.

    The live ``Gate`` atoms are what ``test.sh`` later scores with. Re-running
    every probe here needs the vendored runtime and turns a missing pretest
    fixture into an infrastructure error before any candidate is graded. This
    check only materialises the two classification canaries and asks the same
    atoms the grader uses: a skip/only marker is an ordinary failure, and a
    weakened assertion stays on the cheating atom.
    """
    import hardgate  # noqa: E402  (local verifier helper)

    scratch = Path(tempfile.mkdtemp(prefix="migration-canary-startup-"))
    materialise_base(repo, base_commit, scratch)
    git_init_commit(scratch)
    checks: List[Dict[str, Any]] = []

    def record(name: str, ok: bool, detail: Dict[str, Any]) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    def gate_for(patch_text: str) -> "hardgate.Gate":
        parser = argparse.ArgumentParser()
        parser.add_argument("--config")
        parser.add_argument("--repo")
        parser.add_argument("--patch-file")
        parser.add_argument("--output")
        parser.add_argument("--logs-dir")
        parser.add_argument("--tests-dir")
        args = parser.parse_args([
            "--config", str(tests_dir / "config.json"),
            "--repo", str(scratch),
            "--patch-file", str(scratch / ".startup.patch"),
            "--output", str(scratch / "hard-gate.json"),
            "--logs-dir", str(scratch / "logs"),
            "--tests-dir", str(tests_dir),
        ])
        gate = hardgate.Gate(args)
        gate.config = json.loads((tests_dir / "config.json").read_text(encoding="utf-8"))
        gate.patch_text = patch_text
        gate.changed_paths = []
        for line in patch_text.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                target = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else ""
                if target:
                    gate.changed_paths.append(target)
        return gate

    spec_rel = Path("test") / "fileLimitUploads.spec.js"
    original = (scratch / spec_rel).read_bytes()

    def patch_after(edit) -> str:
        (scratch / spec_rel).write_bytes(original)
        edit(scratch)
        diff = run(["git", "diff", "--binary", "--", str(spec_rel)], cwd=scratch)
        (scratch / spec_rel).write_bytes(original)
        return diff.stdout.decode("utf-8", "replace")

    def classify(name: str, patch_text: str, expect_tamper: str, expect_skip: str) -> None:
        gate = gate_for(patch_text)
        gate.atom_tests_not_tampered(scratch)
        gate.atom_no_new_skip_or_xfail()
        atoms = gate.results["checks"]["test_integrity"]["atoms"]
        tamper = atoms["tests_not_tampered"]["status"]
        skip = atoms["no_new_skip_or_xfail"]["status"]
        record(name, tamper == expect_tamper and skip == expect_skip, {
            "tests_not_tampered": tamper,
            "no_new_skip_or_xfail": skip,
            "changed_paths": gate.changed_paths,
        })

    # Appending a marker leaves every existing assertion in place. Replacing
    # ``it(`` with ``it.skip(`` is the same ordinary skip failure, not tampering.
    classify("skip_marker", patch_after(
        lambda repo: _append(repo / "test" / "fileLimitUploads.spec.js",
                             "\nit.skip('skip only adversarial case', () => {});\n")),
        "pass", "fail")
    classify("protected_test_edit", patch_after(_weaken_test_assertion), "fail", "fail")
    if ("test_integrity", "tests_not_tampered") not in hardgate.CHEAT_ATOMS:
        record("cheat_atom_present", False, {"tests_not_tampered_in_CHEAT_ATOMS": False})
    failures = sum(1 for check in checks if not check["ok"])
    return {"schema_version": "migration-canary-startup/v1", "failures": failures,
            "checks": checks, "work_dir": str(scratch)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("SWEBENCH_REPO_PATH", "/workspace/repo"))
    parser.add_argument("--tests-dir", default=str(HERE.parent))
    parser.add_argument("--base-commit", default=None)
    parser.add_argument("--node", default=os.environ.get("MIGRATION_NODE"))
    parser.add_argument("--solution-patch", default=None)
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--json-output", default=None)
    parser.add_argument("--only", action="append", default=None)
    parser.add_argument("--security-selfcheck", action="store_true",
                        help="also assert the evaluation assets are unreadable by the candidate identity")
    parser.add_argument("--startup-selfcheck", action="store_true",
                        help="lightweight pre-score check: skip-only must be an ordinary "
                             "failure and assertion weakening must stay cheating")
    args = parser.parse_args()

    config = json.loads((Path(args.tests_dir) / "config.json").read_text(encoding="utf-8"))
    base_commit = args.base_commit or config.get("base_commit")
    if not base_commit:
        print("[canary] config.base_commit is missing", file=sys.stderr)
        return 2

    args.repo = str(Path(args.repo).resolve())
    if args.startup_selfcheck:
        report = startup_selfcheck(Path(args.repo), Path(args.tests_dir), base_commit)
        for check in report["checks"]:
            print(f"[startup] {check['name']:30s} {'ok' if check['ok'] else 'FAIL':5s} "
                  f"{json.dumps(check['detail'], ensure_ascii=False)}")
        if args.json_output:
            Path(args.json_output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                              encoding="utf-8")
        print(f"[canary] startup self-check failures={report['failures']}")
        return 1 if report["failures"] else 0
    if args.solution_patch:
        args.solution_patch = str(Path(args.solution_patch).resolve())
    workdir = Path(args.work_dir or tempfile.mkdtemp(prefix="migration-canary-"))
    workdir.mkdir(parents=True, exist_ok=True)
    node_modules = Path(args.repo) / "node_modules"

    security: List[Dict[str, Any]] = []
    if args.security_selfcheck:
        security = security_selfcheck(Path(args.tests_dir), os.environ.get("MIGRATION_SANDBOX_USER"))
        for check in security:
            print(f"[security] {check['name']:40s} {'ok' if check['ok'] else 'FAIL':5s} {check['detail']}")

    results: List[Dict[str, Any]] = []
    for variant in build_variants(Path(args.repo), Path(args.solution_patch) if args.solution_patch else None):
        if args.only and variant["name"] not in args.only:
            continue
        try:
            results.append(evaluate_variant(args, variant, workdir, base_commit, node_modules))
        except Exception as err:  # noqa: BLE001
            results.append({"name": variant["name"], "error": str(err), "ok": False})

    print(f"{'canary':30s} {'result':8s} expected -> actual")
    failures = 0
    for item in results:
        if item.get("skipped"):
            print(f"{item['name']:30s} SKIP     {item['skipped']}")
            continue
        if "error" in item:
            failures += 1
            print(f"{item['name']:30s} ERROR    {item['error']}")
            continue
        flag = "ok" if item["ok"] else "MISMATCH"
        if not item["ok"]:
            failures += 1
        print(f"{item['name']:30s} {flag:8s} {json.dumps(item['expected'], ensure_ascii=False)} -> "
              f"{json.dumps(item['actual'], ensure_ascii=False)}")

    if security:
        failures += sum(1 for check in security if not check["ok"])
    report = {"schema_version": "migration-canary-suite/v1", "base_commit": base_commit,
              "work_dir": str(workdir), "total": len(results), "failures": failures,
              "security": security, "results": results}
    if args.json_output:
        Path(args.json_output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[canary] {len(results) - failures}/{len(results)} canaries matched their expected verdict")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

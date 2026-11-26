"""Regression guard for the delivered standalone verifier gate.

The r6 norm report (R7-QC-3 / R7-QC-7) rejected ``tests/verifier/verifier.py``
because it referenced undefined functions/modules and read a legacy
``config["target_environment"]`` key that no longer exists.  This self-check
pins the repaired contract:

* the file compiles and has no unresolved names;
* none of the legacy Docker/ledger names or the removed config key return;
* the gate still exposes the ``--candidate-root`` / ``--config`` / ``--output``
  CLI and delegates to ``run_harbor_verifier``.

Run it from anywhere::

    python3 environment/verifier_entry_selfcheck.py

It is dependency-free and never starts Docker.
"""

from __future__ import annotations

import ast
import builtins
import subprocess
import sys
from pathlib import Path

_TASK_ROOT = Path(__file__).resolve().parents[1]
_TESTS = _TASK_ROOT / "tests"
_VERIFIER = _TESTS / "verifier" / "verifier.py"

# Names the rejected legacy implementation referenced without defining them.
_LEGACY_NAMES = (
    "run_docker_build",
    "label_arguments",
    "resource_labels",
    "record_current_resource",
    "current_resource_ledger",
)
_LEGACY_CONFIG_KEY = "target_environment"
_REQUIRED_ARGS = ("--candidate-root", "--config", "--output")


def _check(name: str, condition: object, detail: object = "") -> None:
    if not condition:
        raise RuntimeError(f"{name}: {detail}")


def _defined_names(tree: ast.AST) -> set[str]:
    defined = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                defined.add(alias.asname or alias.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
    return defined


def _check_source(source: str) -> None:
    try:
        tree = ast.parse(source, filename=str(_VERIFIER))
    except SyntaxError as exc:  # pragma: no cover - surfaces as a check failure
        raise RuntimeError(f"source.syntax: {exc}") from exc

    for legacy in (*_LEGACY_NAMES, _LEGACY_CONFIG_KEY):
        _check(f"source.no-legacy-{legacy}", legacy not in source, legacy)

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    _check("source.delegates", "run_harbor_verifier" in imported, imported)

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    _check("source.calls-delegation", "run_harbor_verifier" in called, called)

    defined = _defined_names(tree)
    loads = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    unresolved = sorted(loads - defined)
    _check("source.unresolved-names", not unresolved, unresolved)


def _check_help() -> None:
    for cwd in (_TESTS, _TESTS / "verifier"):
        completed = subprocess.run(
            [sys.executable, "-B", str(_VERIFIER), "--help"],
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        _check(f"help.returncode.{cwd.name}", completed.returncode == 0, completed.stderr[-500:])
        output = completed.stdout + completed.stderr
        for argument in _REQUIRED_ARGS:
            _check(f"help.{argument}", argument in output, output)


def main() -> int:
    _check("gate.exists", _VERIFIER.is_file(), _VERIFIER)
    source = _VERIFIER.read_text(encoding="utf-8")
    _check("gate.nonempty", bool(source.strip()))
    _check_source(source)
    _check_help()
    print("verifier_entry_selfcheck: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

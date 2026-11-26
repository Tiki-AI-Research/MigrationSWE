"""Regression guard: the delivered task tree must stay free of Python caches.

The r8 structure snapshot found ``tests/verifier/__pycache__/integrity.cpython-312.pyc``
inside the shipped task tree.  ``tests/grader.py`` imported ``verifier.harbor_entry``
before disabling bytecode emission, so the default interpreter dropped compiled
artifacts for the verifier's sibling modules into the delivered bundle.

This guard pins the structural invariant that no interpreter cache may live under
the task root:

* no ``__pycache__`` directory anywhere under the task root;
* no ``*.pyc`` / ``*.pyo`` file anywhere under the task root;
* it emits no bytecode of its own (``sys.dont_write_bytecode`` plus stdlib only).

Run it from anywhere::

    python3 environment/tree_hygiene_selfcheck.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True

_TASK_ROOT = Path(__file__).resolve().parents[1]

_CACHE_DIR = "__pycache__"
_BYTECODE_SUFFIXES = (".pyc", ".pyo")


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(_TASK_ROOT.rglob("*")):
        relative = path.relative_to(_TASK_ROOT).as_posix()
        if path.is_dir() and path.name == _CACHE_DIR:
            found.append(relative + "/")
        elif path.is_file() and path.suffix in _BYTECODE_SUFFIXES:
            found.append(relative)
    return found


def main() -> int:
    offenders = _offenders()
    if offenders:
        raise SystemExit("tree_hygiene_selfcheck: FAIL\n" + "\n".join(offenders))
    print("tree_hygiene_selfcheck: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

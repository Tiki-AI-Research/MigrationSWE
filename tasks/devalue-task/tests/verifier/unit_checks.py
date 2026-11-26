"""Deterministic static validation of migration-unit target conditions.

Every migration unit declares a target condition and target locators.  This
module verifies those declarations directly against the candidate tree so a
unit can never pass from self-reported observation rows alone.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# The public entry points the source (4.3.3) package already exposed.  The hard
# gate ``public_contract_preserved`` may only require these four: a missing
# 6.0.0 addition is an *incomplete migration* that scope (MU003/MU011) and the
# F2P observations score with a partial ``T``, not a broken pre-existing
# contract that warrants a hard-gate zero.
PRESERVED_PUBLIC_EXPORTS = (
    "uneval",
    "parse",
    "unflatten",
    "stringify",
)

# Full 6.0.0 surface, including the additions the migration must introduce.
# Scope unit checks (MU003/MU011) use this tuple; the hard gate does not.
PUBLIC_EXPORTS = (
    *PRESERVED_PUBLIC_EXPORTS,
    "stringifyAsync",
    "defaultStringifyOperations",
    "defaultParseOperations",
    "DevalueError",
    "filterArrayIndices",
)

DEPENDENCY_FIELDS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)


def _check(identifier: str, ok: bool, detail: str) -> dict[str, str]:
    return {"id": identifier, "status": "PASS" if ok else "FAIL", "detail": detail}


def _text(root: Path, relative: str) -> str | None:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _manifest(root: Path, relative: str = "package.json") -> Mapping[str, Any] | None:
    text = _text(root, relative)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, Mapping) else None


def _declared_type_entries(manifest: Mapping[str, Any] | None) -> list[str]:
    if manifest is None:
        return []
    entries: list[str] = []
    top_level = manifest.get("types")
    if isinstance(top_level, str) and top_level.strip():
        entries.append(top_level.strip())
    exports = manifest.get("exports")
    root_export = exports.get(".") if isinstance(exports, Mapping) else None
    if isinstance(root_export, Mapping):
        declared = root_export.get("types")
        if isinstance(declared, str) and declared.strip():
            entries.append(declared.strip())
    return list(dict.fromkeys(entries))


def _read_declaration_entry(root: Path, declared: str) -> tuple[str | None, str]:
    if "\x00" in declared or "\\" in declared:
        return None, "declared type entry contains an unsafe character"
    normalized = declared[2:] if declared.startswith("./") else declared
    relative = Path(normalized)
    if not normalized or relative.is_absolute() or ".." in relative.parts:
        return None, "declared type entry must stay inside the package"
    current = root
    for part in relative.parts:
        if part in {"", "."}:
            continue
        current = current / part
        if current.is_symlink():
            return None, "declared type entry must not traverse a symlink"
    if not current.is_file():
        return None, f"declared type entry is not a readable regular file: {declared}"
    try:
        text = current.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, f"declared type entry is not readable: {declared}"
    if not re.search(r"\bexport\b", text):
        return None, f"declared type entry has no public export: {declared}"
    return text, declared


def _type_entry_checks(root: Path, prefix: str) -> list[dict[str, str]]:
    manifest = _manifest(root)
    entries = _declared_type_entries(manifest)
    if not entries:
        return [
            _check(
                f"{prefix}-types-declared",
                False,
                "package.json must declare types or exports['.'].types",
            )
        ]
    checks: list[dict[str, str]] = []
    for entry in entries:
        text, detail = _read_declaration_entry(root, entry)
        checks.append(
            _check(
                f"{prefix}-types-entry:{entry}",
                text is not None,
                detail,
            )
        )
    return checks


def _exists(root: Path, relative: str) -> bool:
    path = root / relative
    return path.exists() and not path.is_symlink()


def _has(text: str | None, needle: str) -> bool:
    return bool(text) and needle in text


def _declared_specifiers(manifest: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for field in DEPENDENCY_FIELDS:
        entries = manifest.get(field)
        if isinstance(entries, Mapping):
            values.extend(str(value) for value in entries.values())
    return values


def _scripts(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    scripts = manifest.get("scripts")
    return scripts if isinstance(scripts, Mapping) else {}


def _declares_export(text: str | None, name: str) -> bool:
    if not text:
        return False
    escaped = re.escape(name)
    if re.search(r"\bexport\s+(?:async\s+)?(?:function|const|let|var|class)\s+" + escaped + r"\b", text):
        return True
    for block in re.finditer(r"\bexport\s*\{([^}]*)\}", text, re.DOTALL):
        for clause in block.group(1).split(","):
            parts = clause.split(" as ")
            exported = parts[-1].strip()
            if exported == name:
                return True
    return False


def _declared_names(text: str | None) -> set[str]:
    if not text:
        return set()
    names = set(
        re.findall(
            r"\bexport\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)",
            text,
        )
    )
    for block in re.finditer(r"\bexport\s*\{([^}]*)\}", text, re.DOTALL):
        for clause in block.group(1).split(","):
            parts = clause.split(" as ")
            names.add(parts[-1].strip())
    return {name for name in names if name}


def _module_exports(root: Path, relative: str, seen: set[str] | None = None) -> set[str]:
    """Resolve the public names reachable from a module's export syntax."""

    seen = seen if seen is not None else set()
    relative = relative.replace("\\", "/").lstrip("./")
    if relative in seen:
        return set()
    seen.add(relative)
    text = _text(root, relative)
    if text is None:
        return set()
    names = _declared_names(text)
    for match in re.finditer(
        r"\bexport\s*\*\s*from\s*['\"]([^'\"]+)['\"]", text
    ):
        specifier = match.group(1)
        if specifier.startswith("."):
            names |= _module_exports(
                root,
                (Path(relative).parent / specifier).as_posix(),
                seen,
            )
    for match in re.finditer(
        r"\bexport\s*\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]",
        text,
        re.DOTALL,
    ):
        for clause in match.group(1).split(","):
            parts = clause.split(" as ")
            exported = parts[-1].strip()
            if exported:
                names.add(exported)
    return names


def _public_names(root: Path) -> set[str]:
    names = _module_exports(root, "index.js")
    manifest = _manifest(root)
    if manifest is not None:
        exports = manifest.get("exports")
        root_export = exports.get(".") if isinstance(exports, Mapping) else None
        if isinstance(root_export, str):
            names |= _module_exports(root, root_export)
        elif isinstance(root_export, Mapping):
            for value in root_export.values():
                if isinstance(value, str):
                    names |= _module_exports(root, value)
    return names


def _export_check(root: Path, identifier: str, name: str) -> dict[str, str]:
    exported = name in _public_names(root)
    return _check(
        identifier,
        exported,
        f"the public package entry must export {name}",
    )


def _public_surface_check(
    root: Path,
    identifier: str,
    names: Sequence[str],
    requirement_ids: Sequence[str],
) -> dict[str, str]:
    exported = _public_names(root)
    missing = [name for name in names if name not in exported]
    requirements = ", ".join(requirement_ids)
    return _check(
        identifier,
        not missing,
        (
            f"{requirements}: the public package entry must expose "
            f"{', '.join(names)}"
            + (f"; missing {', '.join(missing)}" if missing else "")
        ),
    )


def _script_check(
    root: Path,
    identifier: str,
    name: str,
    requirement_ids: Sequence[str],
) -> dict[str, str]:
    manifest = _manifest(root)
    scripts = _scripts(manifest) if manifest is not None else {}
    requirements = ", ".join(requirement_ids)
    return _check(
        identifier,
        bool(str(scripts.get(name, "")).strip()),
        f"{requirements}: package.json must define an executable {name} script",
    )


def _mu001(root: Path) -> list[dict[str, str]]:
    manifest = _manifest(root)
    checks = [
        _check("mu001-manifest-readable", manifest is not None, "package.json must be readable JSON"),
    ]
    if manifest is None:
        return checks
    scripts = _scripts(manifest)
    checks.extend(
        [
            _check(
                "mu001-version-6.0.0",
                str(manifest.get("version")) == "6.0.0",
                f"package.json version must be 6.0.0, got {manifest.get('version')!r}",
            ),
            _check(
                "mu001-test-script",
                bool(str(scripts.get("test", "")).strip()),
                "the package must define an executable test script",
            ),
            _check(
                "mu001-test-types-script",
                "test:types" in scripts,
                "the package must define a test:types script",
            ),
            _check(
                "mu001-no-legacy-version-specifier",
                not any("4.3.3" in item for item in _declared_specifiers(manifest)),
                "no dependency specifier may target devalue 4.3.3",
            ),
        ]
    )
    return checks


def _mu002(root: Path) -> list[dict[str, str]]:
    lock = _text(root, "pnpm-lock.yaml")
    return [
        _check("mu002-lockfile-present", lock is not None, "pnpm-lock.yaml must exist"),
        _check(
            "mu002-no-old-version",
            not _has(lock, "devalue@4.3.3"),
            "the lockfile must not resolve devalue 4.3.3",
        ),
    ]


def _mu003(root: Path) -> list[dict[str, str]]:
    names = _public_names(root)
    return [
        _check(
            f"mu003-export-{name}",
            name in names,
            f"the public package entry must export {name}",
        )
        for name in PUBLIC_EXPORTS
    ]


def _mu004(root: Path) -> list[dict[str, str]]:
    return [
        _export_check(root, "mu004-stringify-export", "stringify"),
        _export_check(root, "mu004-stringify-async-export", "stringifyAsync"),
    ]


def _mu005(root: Path) -> list[dict[str, str]]:
    return [
        _export_check(root, "mu005-parse-export", "parse"),
        _export_check(root, "mu005-unflatten-export", "unflatten"),
    ]


def _mu006(root: Path) -> list[dict[str, str]]:
    return [_export_check(root, "mu006-uneval-export", "uneval")]


def _mu007(root: Path) -> list[dict[str, str]]:
    return [
        _export_check(root, "mu007-stringify-operations-exported", "defaultStringifyOperations"),
        _export_check(root, "mu007-parse-operations-exported", "defaultParseOperations"),
    ]


def _mu008(root: Path) -> list[dict[str, str]]:
    return [
        _export_check(root, "mu008-devalue-error-export", "DevalueError"),
        _export_check(
            root, "mu008-filter-array-indices-export", "filterArrayIndices"
        ),
    ]


def _mu009(root: Path) -> list[dict[str, str]]:
    return _type_entry_checks(root, "mu009")


def _mu010(root: Path) -> list[dict[str, str]]:
    return [
        _public_surface_check(
            root,
            "mu010-binary-roundtrip-surface",
            ("stringify", "parse"),
            ("R008",),
        ),
        _script_check(root, "mu010-executable-regression", "test", ("R008",)),
    ]


def _mu011(root: Path) -> list[dict[str, str]]:
    return [
        _public_surface_check(
            root,
            "mu011-public-contract-surface",
            PUBLIC_EXPORTS,
            ("R014",),
        )
    ]


def assertion_helper_imports(root: Path) -> list[tuple[str, str]]:
    """Return executable assertion imports in the preserved source suite."""
    try:
        from .integrity import _scan_javascript
    except ImportError:
        from integrity import _scan_javascript
    executable = _scan_javascript(_text(root, "test/test.js") or "")[2]
    assertion_names = (
        "assert|expect|deepStrictEqual|strictEqual|equal|is|ok|throws|"
        "instance|type|unreachable"
    )
    return [
        match.groups() for match in re.finditer(
            r"\bimport\s+([^;]+?)\s+from\s*['\"]([^'\"]+)['\"]", executable
        ) if re.search(rf"\b(?:{assertion_names})\b", match.group(1))
    ]


def _assertion_helper_checks(root: Path) -> list[dict[str, str]]:
    """The preserved suite must obtain its assertions from one local module.

    Registration can come from Vitest, but importing assertions directly from
    Vitest/node:assert does not satisfy the explicit helper-module contract.
    Runtime native tests and the existing assertion-integrity checks continue
    to check assertion contents; this check only enforces the module boundary.
    """
    helpers: set[Path] = set()
    invalid: list[str] = []
    for bindings, specifier in assertion_helper_imports(root):
        if not specifier.startswith("."):
            invalid.append(specifier)
            continue
        target = (root / "test" / specifier).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file() or target.is_symlink():
            invalid.append(specifier)
        else:
            helpers.add(target)
    return [_check(
        "mu012-single-local-assertion-helper", len(helpers) == 1 and not invalid,
        "the preserved suite must import all assert/expect helpers from one local module"
        + (f"; external or invalid imports: {invalid}" if invalid else ""),
    )]


def _mu012(root: Path) -> list[dict[str, str]]:
    manifest = _manifest(root)
    scripts = _scripts(manifest) if manifest is not None else {}
    return [
        _check(
            "mu012-test-script",
            bool(str(scripts.get("test", "")).strip()),
            "the package must define an executable test script",
        ),
        _check(
            "mu012-test-types-script",
            "test:types" in scripts,
            "the package must define a test:types script",
        ),
        *_assertion_helper_checks(root),
    ]


def _mu013(root: Path) -> list[dict[str, str]]:
    return [
        _public_surface_check(
            root,
            "mu013-serialization-regression-surface",
            ("stringify", "stringifyAsync", "parse", "unflatten"),
            ("R001", "R008", "R010", "R011", "R017"),
        ),
        _script_check(
            root,
            "mu013-executable-regression",
            "test",
            ("R001", "R009", "R016"),
        ),
    ]


def _mu014(root: Path) -> list[dict[str, str]]:
    return [
        _public_surface_check(
            root,
            "mu014-operation-map-surface",
            ("defaultStringifyOperations", "defaultParseOperations"),
            ("R005", "R006"),
        ),
        _script_check(root, "mu014-executable-regression", "test", ("R005", "R006")),
    ]


def _mu015(root: Path) -> list[dict[str, str]]:
    return [
        _public_surface_check(
            root,
            "mu015-parse-regression-surface",
            ("parse", "unflatten"),
            ("R003", "R011"),
        ),
        _script_check(root, "mu015-executable-regression", "test", ("R003", "R011")),
    ]


def _mu016(root: Path) -> list[dict[str, str]]:
    return [
        _public_surface_check(
            root,
            "mu016-special-value-surface",
            ("stringify", "parse"),
            ("R008",),
        ),
        _script_check(root, "mu016-executable-regression", "test", ("R008",)),
    ]


def _mu017(root: Path) -> list[dict[str, str]]:
    return [
        _public_surface_check(
            root,
            "mu017-error-and-filter-surface",
            ("DevalueError", "filterArrayIndices"),
            ("R006", "R011"),
        ),
        _script_check(root, "mu017-executable-regression", "test", ("R006", "R011")),
    ]


def _mu018(root: Path) -> list[dict[str, str]]:
    return [
        _script_check(
            root,
            "mu018-typecheck-script",
            "test:types",
            ("R013",),
        ),
        _check(
            "mu018-types-surface",
            _manifest(root) is not None,
            "R013: package.json must remain readable so the public types entry can be resolved",
        ),
    ]


def _mu021(root: Path) -> list[dict[str, str]]:
    manifest = _manifest(root)
    scripts = _scripts(manifest) if manifest is not None else {}
    return [
        _check(
            "mu021-test-and-typecheck-workflow",
            bool(str(scripts.get("test", "")).strip())
            and bool(str(scripts.get("test:types", "")).strip()),
            (
                "R014: the package must retain executable test and test:types "
                "commands used by CI"
            ),
        )
    ]


def _mu022(root: Path) -> list[dict[str, str]]:
    # The task contract requires the public API to remain importable and
    # callable; it does not require release notes or a CHANGELOG version
    # heading.  Keep only the baseline files' presence so a candidate is not
    # failed for omitting documentation the instruction never asked it to write.
    return [
        _check("mu022-readme-present", _exists(root, "README.md"), "README.md must exist"),
        _check(
            "mu022-changelog-present",
            _exists(root, "CHANGELOG.md"),
            "CHANGELOG.md must exist",
        ),
    ]


def _mu024(root: Path) -> list[dict[str, str]]:
    # The 6.0.0 replacer contract is observable through the public uneval
    # export; the fragment implementation lives behind it and is not
    # constrained.
    return [_export_check(root, "mu024-uneval-export", "uneval")]


def _mu025(root: Path) -> list[dict[str, str]]:
    # UTF-8 safety is observable on every public serialization surface.
    return [
        _export_check(root, "mu025-stringify-export", "stringify"),
        _export_check(root, "mu025-stringify-async-export", "stringifyAsync"),
        _export_check(root, "mu025-uneval-export", "uneval"),
    ]


TYPE_SURFACE_EXPORTS = ("JavaScriptFragment", "JavaScriptTag", "UnevalReplacer")


def _declared_export_names(root: Path, entry: str) -> tuple[set[str] | None, str]:
    """Resolve exports, including barrels, aliases and cycles, with TypeScript.

    The compiler is evaluator-owned. Never import candidate JavaScript or load
    a compiler from the candidate's node_modules to validate its own result.
    """
    try:
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("type-exports.cjs")),
             str(root.resolve()), entry],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"cannot run evaluator TypeScript export resolution: {exc}"
    if result.returncode != 0:
        return None, "evaluator TypeScript export resolution failed: " + result.stderr[-1000:]
    try:
        payload = json.loads(result.stdout)
        names = payload["names"]
        diagnostics = payload["diagnostics"]
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise ValueError("invalid export names")
        if not isinstance(diagnostics, list):
            raise ValueError("invalid compiler diagnostics")
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"invalid evaluator TypeScript export result: {exc}"
    if diagnostics:
        return set(), "declaration resolution errors: " + json.dumps(diagnostics, ensure_ascii=False)[:2000]
    return set(names), "resolved by the evaluator TypeScript compiler"


def _mu026(root: Path) -> list[dict[str, str]]:
    # The 6.0.0 public type surface is observable from the declared type
    # entry: the three new names must be exported, not merely mentioned.  The
    # private declaration layout and the generating tool remain free.
    manifest = _manifest(root)
    entries = _declared_type_entries(manifest)
    if not entries:
        return [
            _check(
                "mu026-types-declared",
                False,
                "package.json must declare types or exports['.'].types",
            )
        ]
    checks: list[dict[str, str]] = []
    for entry in entries:
        text, detail = _read_declaration_entry(root, entry)
        if text is None:
            checks.append(_check(f"mu026-types-entry:{entry}", False, detail))
            continue
        names, detail = _declared_export_names(root, entry)
        if names is None:
            checks.append({"id": f"mu026-types-resolution:{entry}",
                           "status": "INCONCLUSIVE", "detail": detail})
            continue
        for name in TYPE_SURFACE_EXPORTS:
            exported = name in names
            checks.append(
                _check(
                    f"mu026-export:{name}",
                    exported,
                    f"the declared type entry must export {name}; {detail}",
                )
            )
    return checks


_UNIT_CHECKS: dict[str, Callable[[Path], list[dict[str, str]]]] = {
    "MU001": _mu001,
    "MU002": _mu002,
    "MU003": _mu003,
    "MU004": _mu004,
    "MU005": _mu005,
    "MU006": _mu006,
    "MU007": _mu007,
    "MU008": _mu008,
    "MU009": _mu009,
    "MU010": _mu010,
    "MU011": _mu011,
    "MU012": _mu012,
    "MU013": _mu013,
    "MU014": _mu014,
    "MU015": _mu015,
    "MU016": _mu016,
    "MU017": _mu017,
    "MU018": _mu018,
    "MU021": _mu021,
    "MU022": _mu022,
    "MU024": _mu024,
    "MU025": _mu025,
    "MU026": _mu026,
}


def _locator_checks(unit: Mapping[str, Any], root: Path) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    for field in ("target_locators",):
        locators = unit.get(field, [])
        if not isinstance(locators, Sequence) or isinstance(locators, (str, bytes)):
            continue
        for locator in locators:
            if not isinstance(locator, Mapping):
                continue
            if locator.get("required") is not True:
                # A target locator is a hint unless the requirement explicitly
                # marks it as required; internal paths are not a contract.
                continue
            path = str(locator.get("path") or "").strip("/")
            if not path:
                checks.append(
                    _check(f"{field}:<empty>", False, f"{field} entries require a path")
                )
                continue
            checks.append(
                _check(
                    f"{field}:{path}",
                    _exists(root, path),
                    f"{field} path {path} must exist in the candidate",
                )
            )
    return checks


def evaluate_unit_static_checks(unit: Mapping[str, Any], root: Path | None) -> dict[str, Any]:
    """Return the deterministic static verdict for one migration unit."""

    unit_id = str(unit.get("id") or "")
    if root is None:
        return {
            "status": "INCONCLUSIVE",
            "unit_id": unit_id,
            "checks": [],
            "error": "candidate root is unavailable for static checks",
        }
    checks = _locator_checks(unit, root)
    specific = _UNIT_CHECKS.get(unit_id)
    if specific is None:
        return {
            "status": "INCONCLUSIVE",
            "unit_id": unit_id,
            "checks": checks,
            "error": f"no deterministic target-condition checks are defined for {unit_id}",
        }
    checks.extend(specific(root))
    if not checks:
        return {
            "status": "INCONCLUSIVE",
            "unit_id": unit_id,
            "checks": [],
            "error": (
                f"no executable requirement-bound evidence is available for {unit_id}; "
                "the unit cannot pass from an empty check set"
            ),
        }
    status = (
        "FAIL" if any(row["status"] == "FAIL" for row in checks)
        else "INCONCLUSIVE" if any(row["status"] == "INCONCLUSIVE" for row in checks)
        else "PASS"
    )
    return {"status": status, "unit_id": unit_id, "checks": checks}


def evaluate_public_contract(root: Path | None) -> dict[str, Any]:
    """Verify the documented public export contract statically.

    This feeds the ``public_contract_preserved`` hard-gate atom and therefore
    only guards the exports the source package already provided.  The 6.0.0
    additions are migration targets handled by scope and F2P; requiring them
    here would turn an incomplete migration into a hard-gate zero.
    """

    if root is None:
        return {"status": "INCONCLUSIVE", "checks": [], "error": "candidate root is unavailable"}
    names = _public_names(root)
    manifest = _manifest(root)
    checks = [
        _check(
            f"contract-export-{name}",
            name in names,
            f"the public package entry must export {name}",
        )
        for name in PRESERVED_PUBLIC_EXPORTS
    ]
    exports = manifest.get("exports") if manifest is not None else None
    root_export = exports.get(".") if isinstance(exports, Mapping) else None
    checks.append(
        _check(
            "contract-export-map",
            isinstance(root_export, (str, Mapping)),
            "package.json must keep a root export entry",
        )
    )
    checks.extend(_type_entry_checks(root, "contract"))
    status = "FAIL" if any(row["status"] == "FAIL" for row in checks) else "PASS"
    return {"status": status, "checks": checks}


__all__ = [
    "PUBLIC_EXPORTS",
    "PRESERVED_PUBLIC_EXPORTS",
    "evaluate_public_contract",
    "evaluate_unit_static_checks",
]

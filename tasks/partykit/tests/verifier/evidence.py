"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .residue import _javascript_analysis, classify_residue_rules, scan_residue_rules
except ImportError:  # Standalone Runtime Draft layout.
    from residue import _javascript_analysis, classify_residue_rules, scan_residue_rules


_TEST_NAME = re.compile(
    r"(?:^|/)(?:test[^/]*|[^/]*(?:\.test|\.spec)\.[^/]*"
    r"|[^/]*_(?:test|spec)\.[^/]+|[^/]*(?:Test|Tests|Spec)\.[^/]+)$",
    re.IGNORECASE,
)
_SKIP_MARKERS = (
    re.compile(r"pytest\.mark\.skip", re.IGNORECASE),
    re.compile(r"pytest\.mark\.xfail", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.skip\s*\(", re.IGNORECASE),
    re.compile(r"(?:describe|it|test)\.only\s*\(", re.IGNORECASE),
    re.compile(r"@(?:unittest\.)?skip(?:If|Unless)?\b", re.IGNORECASE),
    re.compile(r"\b(?:skip|xfail|disabled)\s*=\s*true\b", re.IGNORECASE),
)
# Mocha disables a suite/spec with x-prefixed aliases (``xdescribe``,
# ``xcontext``, ``xit``, ``xspecify``).  They are semantically identical to
# ``.skip`` and a migration translates them one-for-one, so they must be counted
# symmetrically; otherwise the mechanical ``xdescribe -> describe.skip`` rewrite
# looks like a newly introduced skip.  Only invocations count: a compatibility
# shim that *defines* ``globals.xit = it.skip`` (or references ``xit``) is not a
# disabled test.
_DISABLED_ALIAS = re.compile(r"\bx(?:describe|context|it|specify)\s*\(", re.IGNORECASE)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _is_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    if Path(parts[-1]).suffix.casefold() not in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}:
        return False
    return "test" in {part.casefold() for part in parts[:-1]} or bool(
        _TEST_NAME.search(path)
    )


def _regular_files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        return {}
    values: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if ".git" in Path(relative).parts or "node_modules" in Path(relative).parts:
            continue
        values[relative] = path
    return values


def _scope_policy_hits(root: Path, patterns: object, ignored_paths: object = ()) -> list[dict[str, str]]:
    """Return changed repository paths outside the instruction's edit scope."""
    if not isinstance(patterns, Sequence) or isinstance(patterns, (str, bytes)) or not patterns:
        return []
    changed: list[str] = []
    untracked: list[str] = []
    try:
        changed = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", "--no-renames", "HEAD", "--"],
            check=True, capture_output=True, text=True, timeout=15,
        ).stdout.splitlines()
        untracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"],
            check=True, capture_output=True, text=True, timeout=15,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        # Some public runtime copies omit .git. The evaluator-owned submitted
        # patch remains available and is authoritative for the changed path set.
        pass
    patch = Path("/logs/artifacts/model.patch")
    if patch.is_file() and not patch.is_symlink():
        for line in patch.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("diff --git "):
                continue
            try:
                fields = shlex.split(line)
            except ValueError:
                continue
            if len(fields) == 4:
                changed.extend(path[2:] for path in fields[2:] if path.startswith(("a/", "b/")))
    allowed = [str(item).rstrip("/") for item in patterns if isinstance(item, str) and item]
    ignored = [str(item).strip("/") for item in ignored_paths if isinstance(item, str) and item]
    violations = sorted(set(changed + untracked))
    return [
        {"path": path, "token": "out-of-scope-change", "kind": "scope-policy", "rule_id": "INSTRUCTION-CHANGE-SCOPE"}
        for path in violations
        if not any(fnmatchcase(path, pattern) for pattern in allowed)
        and not any(path == item or path.startswith(item + "/") for item in ignored)
    ]


def _strip_comments(text: str) -> str:
    """Blank comment and string content while preserving offsets.

    Marker counting must not treat prose in comments or string literals as a
    disabled test.  Replacing the skipped characters with spaces keeps line
    numbers and column offsets stable for diagnostics.
    """

    characters = list(text)
    length = len(text)
    state = "normal"
    quote = ""
    index = 0
    while index < length:
        character = text[index]
        if state == "normal":
            if character == "/" and index + 1 < length and text[index + 1] == "/":
                while index < length and text[index] != "\n":
                    characters[index] = " "
                    index += 1
                continue
            if character == "#":
                while index < length and text[index] != "\n":
                    characters[index] = " "
                    index += 1
                continue
            if character == "/" and index + 1 < length and text[index + 1] == "*":
                characters[index] = " "
                characters[index + 1] = " "
                index += 2
                while index < length and not (
                    text[index] == "*"
                    and index + 1 < length
                    and text[index + 1] == "/"
                ):
                    if text[index] != "\n":
                        characters[index] = " "
                    index += 1
                if index < length:
                    characters[index] = " "
                    index += 1
                if index < length:
                    characters[index] = " "
                    index += 1
                continue
            if character in ("'", '"', "`"):
                state = "string"
                quote = character
                index += 1
                continue
            index += 1
            continue
        if character == "\\":
            index += 1
            if index < length:
                if text[index] != "\n":
                    characters[index] = " "
                index += 1
            continue
        if character == quote:
            state = "normal"
            index += 1
            continue
        if character != "\n":
            characters[index] = " "
        index += 1
    return "".join(characters)


def _marker_counts(root: Path, baseline_files: Mapping[str, Any] | None = None) -> dict[str, int]:
    counts = {"skip": 0, "xfail": 0, "disabled": 0}
    for relative, path in _regular_files(root).items():
        if not _is_test(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if path.suffix.casefold() in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
            legacy_text = _strip_comments(text)
            legacy_disabled = sum(
                1 for pattern in (*_SKIP_MARKERS[2:], _DISABLED_ALIAS)
                for _ in pattern.finditer(legacy_text)
            )
            disabled = max(_javascript_analysis(text)["disabled"], legacy_disabled)
            counts["skip"] += len(_SKIP_MARKERS[0].findall(legacy_text))
            counts["xfail"] += len(_SKIP_MARKERS[1].findall(legacy_text))
            record = (baseline_files or {}).get(relative)
            if isinstance(record, Mapping):
                original = text if record.get("sha256") == _digest(path) else None
                if original is None:
                    result = subprocess.run(
                        ["git", "--no-pager", "show", f"HEAD:{relative}"], cwd=root,
                        capture_output=True, check=False, timeout=30,
                    )
                    digest = "sha256:" + hashlib.sha256(result.stdout).hexdigest()
                    if result.returncode == 0 and digest == record.get("sha256"):
                        original = result.stdout.decode("utf-8", errors="ignore")
                if original is not None:
                    legacy_text = _strip_comments(original)
                    legacy = sum(
                        1 for pattern in (*_SKIP_MARKERS[2:], _DISABLED_ALIAS)
                        for _ in pattern.finditer(legacy_text)
                    )
                    disabled -= max(_javascript_analysis(original)["disabled"], legacy) - legacy
            counts["disabled"] += disabled
            continue
        text = _strip_comments(text)
        counts["skip"] += sum(
            1 for pattern in _SKIP_MARKERS[:1] for _ in pattern.finditer(text)
        )
        counts["xfail"] += sum(
            1 for pattern in (_SKIP_MARKERS[1],) for _ in pattern.finditer(text)
        )
        counts["disabled"] += sum(
            1
            for pattern in (*_SKIP_MARKERS[2:], _DISABLED_ALIAS)
            for _ in pattern.finditer(text)
        )
    return counts


def build_source_test_baseline(root: Path) -> dict[str, object]:
    """Freeze source-visible test identities and marker counts for a Case."""

    files = {
        relative: {"sha256": _digest(path)}
        for relative, path in sorted(_regular_files(root).items())
        if _is_test(relative)
    }
    return {
        "schema_version": "migration-source-test-baseline:v1",
        "files": files,
        "marker_counts": _marker_counts(root),
    }


def _atom(
    status: str,
    *,
    source: str,
    evidence: object = None,
) -> dict[str, object]:
    return {
        "status": status,
        "source": source,
        "evidence": [] if evidence is None else evidence,
    }


def _status(value: object, *, source: str, default: str = "INCONCLUSIVE") -> dict[str, object]:
    if isinstance(value, Mapping) and isinstance(value.get("status"), str):
        return {
            "status": str(value["status"]),
            "source": str(value.get("source", source)),
            "evidence": value.get("evidence", []),
        }
    if isinstance(value, bool):
        return _atom("PASS" if value else "FAIL", source=source)
    if value is None:
        return _atom(default, source=source)
    return _atom(str(value), source=source)


def _rows(observations: object) -> list[Mapping[str, Any]]:
    return [
        row for row in observations
        if isinstance(row, Mapping)
    ] if isinstance(observations, Sequence) and not isinstance(observations, (str, bytes)) else []


def _receipt_identity(receipt: Mapping[str, Any]) -> str:
    command = receipt.get("command", [])
    return json.dumps(
        {
            "kind": receipt.get("kind"),
            "phase": receipt.get("phase"),
            "index": receipt.get("index"),
            "command": command,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _derive_execution_atoms(
    receipts: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    accepted_kinds = {
        "environment-build",
        "target-build",
        "target-native-test",
        "dependency-resolution",
        "import-probe",
        "version-probe",
        "target-execution",
        "report",
    }
    valid: list[dict[str, object]] = []
    malformed: list[dict[str, object]] = []
    seen: dict[str, tuple[object, object, object]] = {}
    contradictions: list[dict[str, object]] = []
    for offset, raw in enumerate(receipts):
        receipt = dict(raw)
        kind = receipt.get("kind")
        if (
            receipt.get("schema_version") != "migration-execution-receipt:v1"
            or kind not in accepted_kinds
            or not isinstance(receipt.get("timed_out"), bool)
        ):
            malformed.append({"index": offset, "receipt": receipt})
            continue
        returncode = receipt.get("returncode")
        if returncode is not None and (
            not isinstance(returncode, int) or isinstance(returncode, bool)
        ):
            malformed.append({"index": offset, "receipt": receipt})
            continue
        identity = _receipt_identity(receipt)
        facts = (
            returncode,
            receipt.get("timed_out"),
            receipt.get("report_present"),
        )
        if identity in seen and seen[identity] != facts:
            contradictions.append(
                {"identity": identity, "first": seen[identity], "second": facts}
            )
        else:
            seen[identity] = facts
        valid.append(receipt)
    evidence = {
        "receipts": valid,
        "malformed": malformed,
        "contradictions": contradictions,
    }
    if contradictions:
        atom = _atom("INFRA_ERROR", source="execution-receipts", evidence=evidence)
        return atom, atom, valid
    if any(bool(row.get("timed_out")) for row in valid):
        atom = _atom("INFRA_ERROR", source="execution-receipts", evidence=evidence)
        return atom, atom, valid

    environment = [row for row in valid if row.get("kind") == "environment-build"]
    target_build = [
        row
        for row in valid
        if row.get("kind") in {"target-build", "target-native-test"}
    ]
    dependency = [
        row
        for row in valid
        if row.get("kind") in {"dependency-resolution", "import-probe", "version-probe"}
    ]
    executions = [row for row in valid if row.get("kind") == "target-execution"]
    reports = [row for row in valid if row.get("kind") == "report"]
    report_present = any(row.get("report_present") is True for row in executions)
    report_valid = not reports or all(row.get("valid") is True for row in reports)

    if any(row.get("returncode") not in {0, None} for row in environment):
        build = _atom("INFRA_ERROR", source="environment-build-receipt", evidence=evidence)
    elif any(row.get("returncode") not in {0, None} for row in target_build):
        build = _atom("FAIL", source="target-build-receipt", evidence=evidence)
    elif target_build and all(row.get("returncode") == 0 for row in target_build):
        build = _atom("PASS", source="target-build-receipt", evidence=evidence)
    elif any(row.get("returncode") not in {0, None} for row in executions):
        build = _atom("FAIL", source="target-execution-receipt", evidence=evidence)
    elif report_present and report_valid:
        build = _atom("PASS", source="target-execution-receipt", evidence=evidence)
    else:
        build = _atom("INCONCLUSIVE", source="execution-receipts", evidence=evidence)

    if any(row.get("returncode") not in {0, None} for row in dependency):
        dependencies = _atom(
            "FAIL", source="dependency-probe-receipts", evidence=evidence
        )
    elif dependency and all(row.get("returncode") == 0 for row in dependency):
        dependencies = _atom(
            "PASS", source="dependency-probe-receipts", evidence=evidence
        )
    elif any(row.get("returncode") not in {0, None} for row in executions):
        dependencies = _atom(
            "FAIL", source="target-execution-receipt", evidence=evidence
        )
    elif report_present and report_valid:
        dependencies = _atom(
            "PASS", source="target-execution-receipt", evidence=evidence
        )
    else:
        dependencies = _atom(
            "INCONCLUSIVE", source="execution-receipts", evidence=evidence
        )
    return build, dependencies, valid


def _strip_comments_preserving_strings(text: str) -> str:
    characters = list(text)
    index = 0
    while index < len(text):
        if text[index] == "/" and index + 1 < len(text) and text[index + 1] == "/":
            characters[index] = characters[index + 1] = " "
            index += 2
            while index < len(text) and text[index] != "\n":
                characters[index] = " "
                index += 1
            continue
        if text[index] == "/" and index + 1 < len(text) and text[index + 1] == "*":
            characters[index] = characters[index + 1] = " "
            index += 2
            while index < len(text):
                if text[index] == "*" and index + 1 < len(text) and text[index + 1] == "/":
                    characters[index] = characters[index + 1] = " "
                    index += 2
                    break
                if text[index] != "\n":
                    characters[index] = " "
                index += 1
            continue
        if text[index] in ("'", '"', "`"):
            quote = text[index]
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                elif text[index] == quote:
                    index += 1
                    break
                else:
                    index += 1
            continue
        index += 1
    return "".join(characters)


def _reachable_source_imports(root: Path) -> dict[Path, list[str]]:
    package = (root / "packages/hono-party").resolve()
    entry = package / "src/index.ts"
    if not entry.is_file() or entry.is_symlink():
        return {}
    pending = [entry]
    imports_by_file: dict[Path, list[str]] = {}
    while pending:
        source = pending.pop()
        if source in imports_by_file:
            continue
        imports = _javascript_analysis(source.read_text(encoding="utf-8", errors="replace"))["imports"]
        specifiers = [str(row.get("value", "")) for row in imports]
        imports_by_file[source] = specifiers
        for specifier in specifiers:
            if not specifier.startswith("."):
                continue
            base = source.parent / specifier
            for candidate in (base, *(base.with_suffix(ext) for ext in (".ts", ".tsx", ".js", ".mjs", ".cjs")), *(base / f"index{ext}" for ext in (".ts", ".tsx", ".js"))):
                if candidate.is_file() and not candidate.is_symlink() and candidate.resolve().is_relative_to(package):
                    pending.append(candidate.resolve())
                    break
    return imports_by_file


def _dependency_identity_hits(root: Path) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    package_roots = [path for path in root.glob("packages/*") if path.is_dir()]
    workspace_names: set[str] = set()
    for package_root in package_roots:
        manifest = package_root / "package.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, Mapping) and isinstance(payload.get("name"), str):
            workspace_names.add(payload["name"])
    for package_root in [root / "packages/hono-party"]:
        manifest_path = package_root / "package.json"
        if not package_root.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        declared: set[str] = set()
        if isinstance(manifest, Mapping):
            for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                values = manifest.get(field)
                if isinstance(values, Mapping):
                    declared.update(str(name) for name in values)
        for source, specifiers in _reachable_source_imports(root).items():
            for specifier in specifiers:
                if specifier.startswith((".", "/", "node:", "cloudflare:")):
                    continue
                package_name = "/".join(specifier.split("/")[:2]) if specifier.startswith("@") else specifier.split("/", 1)[0]
                if package_name not in declared and package_name not in workspace_names:
                    hits.append({
                        "path": source.relative_to(root).as_posix(),
                        "token": specifier,
                        "package": package_name,
                        "reason": "production import is not declared by its package manifest",
                    })
    return hits


def _source_dependency_hits(root: Path) -> list[dict[str, str]]:
    manifest_path = root / "packages/hono-party/package.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [{"path": "packages/hono-party/package.json", "token": "invalid-manifest", "reason": "target package manifest cannot be read"}]
    if not isinstance(manifest, Mapping):
        return [{"path": "packages/hono-party/package.json", "token": "invalid-manifest", "reason": "target package manifest is not an object"}]
    hits: list[dict[str, str]] = []
    active = {specifier.split("/", 1)[0] for values in _reachable_source_imports(root).values() for specifier in values}
    for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        dependencies = manifest.get(field, {})
        if not isinstance(dependencies, Mapping):
            continue
        for name, version in dependencies.items():
            source = str(name).casefold() == "hono" or str(version).casefold().startswith("npm:hono@")
            # Constraint 5 requires removal from peer/dev even when unused.
            # Preserve the existing import-based policy for other groups.
            if source and (field in {"peerDependencies", "devDependencies"} or str(name) in active):
                hits.append({
                    "path": "packages/hono-party/package.json",
                    "token": str(name),
                    "field": field,
                    "reason": "source framework dependency remains in target package",
                })
    return hits


def _target_dependency_hits(root: Path) -> list[dict[str, str]]:
    """Validate the target range against the installed, locked dependency.

    The repository lock is an out-of-scope source snapshot. The image's
    installation lock is authoritative for the materialized target modules.
    """
    script = r"""
const fs = require('fs'), path = require('path');
const semver = require('/opt/migration/dependencies/node_modules/semver');
const root = JSON.parse(fs.readFileSync(0, 'utf8'));
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'packages/hono-party/package.json')));
const trusted = '/opt/migration/dependencies';
const local = path.join(root, 'node_modules/elysia/package.json');
const installed = JSON.parse(fs.readFileSync(fs.existsSync(local) ? local : path.join(trusted, 'node_modules/elysia/package.json')));
const lock = JSON.parse(fs.readFileSync(path.join(trusted, 'package-lock.json')));
const locked = lock.packages['node_modules/elysia'];
const hits = [];
for (const field of ['peerDependencies', 'devDependencies']) {
  const range = manifest[field]?.elysia;
  if (typeof range !== 'string' || !semver.validRange(range) || !semver.satisfies('1.3.0', range) || !semver.satisfies(installed.version, range)) hits.push({path:'packages/hono-party/package.json', field, token:'elysia', reason:'target dependency range must include Elysia 1.3.0 and the installed version'});
}
if (installed.name !== 'elysia' || installed.version !== '1.3.0' || !locked || locked.version !== installed.version) hits.push({path:'node_modules/elysia/package.json', token:'elysia', reason:'target installation does not match its trusted lock and target identity'});
process.stdout.write(JSON.stringify(hits));
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps(str(root)), text=True, capture_output=True, timeout=30)
    if result.returncode != 0:
        return [{"path": "packages/hono-party/package.json", "token": "elysia", "reason": "target dependency metadata cannot be resolved"}]
    return json.loads(result.stdout)


def _hidden_source_wrapper_hits(root: Path) -> list[dict[str, str]]:
    entry = (root / "packages/hono-party/src/index.ts").resolve()
    hits: list[dict[str, str]] = []
    delegation_modules = {
        "child_process": "SUBPROCESS",
        "http": "RPC",
        "https": "RPC",
        "http2": "RPC",
        "net": "RPC",
        "tls": "RPC",
        "worker_threads": "WORKER",
        "ffi-napi": "FFI",
        "koffi": "FFI",
        "bun:ffi": "FFI",
    }
    for source, specifiers in _reachable_source_imports(root).items():
        for specifier in specifiers:
            if source != entry and (specifier == "hono" or specifier.startswith("hono/")):
                hits.append({
                    "path": source.relative_to(root).as_posix(),
                    "token": specifier,
                    "kind": "hidden-source-wrapper",
                    "rule_id": "REACHABLE-HONO-WRAPPER",
                })
            module = specifier.removeprefix("node:")
            mechanism = delegation_modules.get(module)
            if mechanism:
                hits.append({
                    "path": source.relative_to(root).as_posix(),
                    "token": specifier,
                    "kind": "runtime-delegation",
                    "rule_id": f"REACHABLE-{mechanism}-DELEGATION",
                })
    return hits


def _group_status(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected: int | None,
    source: str,
    empty: str = "NA",
) -> dict[str, object]:
    if expected == 0 or (expected is None and not rows):
        return _atom(empty, source=source, evidence={"total": 0, "checks": []})
    if not rows:
        return _atom("INCONCLUSIVE", source=source, evidence={"total": 0, "checks": []})
    statuses = [str(row.get("status", "INCONCLUSIVE")) for row in rows]
    status = (
        "INFRA_ERROR" if any(item in {"INFRA_ERROR", "TIMEOUT"} for item in statuses)
        else "INCONCLUSIVE" if any(item in {"INCONCLUSIVE", "NA"} for item in statuses)
        else "FAIL" if any(item == "FAIL" for item in statuses)
        else "PASS"
    )
    return _atom(
        status,
        source=source,
        evidence={
            "passed": sum(item == "PASS" for item in statuses),
            "total": len(statuses),
            "checks": [dict(row) for row in rows],
        },
    )


def _protected_hits(root: Path, protected: Sequence[str]) -> list[str]:
    hits: list[str] = []
    for relative in _regular_files(root):
        normalized = relative.replace("\\", "/")
        for raw in protected:
            path = str(raw).replace("\\", "/").strip("/")
            if path and (
                normalized == path
                or normalized.startswith(path + "/")
                or normalized.startswith(path + ".")
            ):
                hits.append(normalized)
                break
    for raw in protected:
        path = root / Path(str(raw).replace("\\", "/"))
        if path.exists() and str(raw).replace("\\", "/") not in hits:
            hits.append(str(raw).replace("\\", "/").strip("/"))
    return sorted(set(hits))


def _report_marker_hits(root: Path, markers: Sequence[str]) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    if not markers:
        return hits
    for relative, path in _regular_files(root).items():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for marker in markers:
            if str(marker) and str(marker).casefold() in text.casefold():
                hits.append({"path": relative, "marker": str(marker)})
    return hits


def _test_integrity(
    root: Path,
    baseline: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    *,
    source: str = "source-test-baseline",
) -> tuple[dict[str, object], dict[str, object]]:
    if not isinstance(baseline, Mapping):
        inconclusive = _atom("INCONCLUSIVE", source=source)
        return inconclusive, inconclusive
    baseline_files = baseline.get("files", {})
    baseline_files = baseline_files if isinstance(baseline_files, Mapping) else {}
    if not baseline_files:
        # A pass here would be vacuous: there is no test file to protect.  Keep
        # the evidence honest without turning a repository with no target tests
        # into a hard failure.
        empty = _atom(
            "NA",
            source=source,
            evidence={"reason": "baseline-has-no-test-files", "files": 0},
        )
        return empty, empty
    current = _regular_files(root)
    missing = sorted(
        str(relative) for relative in baseline_files
        if str(relative) not in current
    )
    allowed = {
        str(item).replace("\\", "/").strip("/")
        for item in config.get("policy", {}).get("allowed_equivalent_test_paths", [])
    } if isinstance(config.get("policy"), Mapping) else set()
    changed = sorted(
        relative for relative, record in baseline_files.items()
        if relative in current
        and isinstance(record, Mapping)
        and record.get("sha256") != _digest(current[relative])
        and relative not in allowed
    )
    marker_before = baseline.get("marker_counts", {})
    marker_before = marker_before if isinstance(marker_before, Mapping) else {}
    marker_after = _marker_counts(root, baseline_files)
    marker_delta = {
        key: int(marker_after.get(key, 0)) - int(marker_before.get(key, 0))
        for key in {"skip", "xfail", "disabled"}
    }
    tamper = _atom(
        "FAIL" if missing or changed else "PASS",
        source=source,
        evidence={"missing": missing, "changed": changed},
    )
    skip = _atom(
        "FAIL" if any(value > 0 for value in marker_delta.values()) else "PASS",
        source=source,
        evidence={
            "before": dict(marker_before),
            "after": marker_after,
            "delta": marker_delta,
            "skip_only_candidate": bool(not changed and not missing and any(value > 0 for value in marker_delta.values())),
        },
    )
    return tamper, skip


def build_hardgate_evidence(
    candidate_root: Path,
    *,
    config: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    observation_errors: Sequence[str],
    receipts: Sequence[Mapping[str, Any]],
    patch_applicable: bool | None,
    source_test_baseline: Mapping[str, Any] | None,
    test_baseline_source: str = "source-test-baseline",
) -> dict[str, object]:
    """Derive hardgate atoms from Case evidence instead of policy booleans."""

    policy = config.get("policy", {})
    policy = policy if isinstance(policy, Mapping) else {}
    rows = _rows(observations)
    expected_tests = config.get("tests", {})
    expected_tests = expected_tests if isinstance(expected_tests, Mapping) else {}
    p2p = [row for row in rows if row.get("kind") == "P2P"]
    differential = [row for row in rows if row.get("kind") == "DIFFERENTIAL"]
    state = [row for row in rows if row.get("kind") == "STATE"]
    semantic_rules = policy.get("semantic_rules", [])
    residue_rules = policy.get("ordinary_residue_rules", policy.get("residue_rules", semantic_rules))
    residue_diagnostics = classify_residue_rules(candidate_root, residue_rules)
    residue_hits = scan_residue_rules(candidate_root, residue_rules)
    runtime = config.get("runtime", {})
    runtime = runtime if isinstance(runtime, Mapping) else {}
    scope_policy_hits = _scope_policy_hits(
        candidate_root,
        policy.get("allowed_change_paths"),
        runtime.get("writable_storage", []),
    )
    residue_hits.extend(scope_policy_hits)
    deceptive_rules = policy.get("deceptive_fallback_rules", [])
    exemption = policy.get("deceptive_fallback_not_applicable")
    migration = config.get("migration", {})
    category = migration.get("category") if isinstance(migration, Mapping) else None
    missing_l2 = (
        category != "version_upgrade"
        and not deceptive_rules
        and not (
            isinstance(exemption, Mapping)
            and str(exemption.get("reason", "")).strip()
            and str(exemption.get("evidence", "")).strip()
        )
    )
    deceptive_diagnostics = classify_residue_rules(candidate_root, deceptive_rules)
    deceptive_hits = scan_residue_rules(candidate_root, deceptive_rules)
    deceptive_hits.extend(_hidden_source_wrapper_hits(candidate_root))
    protected = [*policy.get("protected_paths", []), *config.get("protected_paths", [])]
    protected = protected if isinstance(protected, Sequence) and not isinstance(protected, (str, bytes)) else []
    marker_names = policy.get("forbidden_report_markers", [])
    marker_names = marker_names if isinstance(marker_names, Sequence) and not isinstance(marker_names, (str, bytes)) else []
    protected_hits = _protected_hits(candidate_root, [str(item) for item in protected])
    marker_hits = _report_marker_hits(candidate_root, [str(item) for item in marker_names])
    tests_not_tampered, no_new_skip = _test_integrity(
        candidate_root, source_test_baseline, config, source=test_baseline_source
    )
    build_evidence, dependency_evidence, valid_receipts = _derive_execution_atoms(
        receipts
    )
    dependency_hits = _dependency_identity_hits(candidate_root) + _source_dependency_hits(candidate_root) + _target_dependency_hits(candidate_root)
    if dependency_hits:
        dependency_evidence = _atom(
            "FAIL", source="declared-dependency-identity", evidence=dependency_hits
        )
    applicability = config.get("group_applicability", {})
    differential_policy = applicability.get("DIFFERENTIAL", {}) if isinstance(applicability, Mapping) else {}
    differential_required = isinstance(differential_policy, Mapping) and differential_policy.get("status") == "required"
    expected_differential = expected_tests.get("differential", [])
    public = _group_status(
        [*p2p, *differential] if differential_required else p2p,
        expected=(len(expected_tests.get("p2p", [])) if isinstance(expected_tests.get("p2p", []), Sequence) else 0)
        + (len(expected_differential) if differential_required and isinstance(expected_differential, Sequence) else 0),
        source="candidate-public-contract-observations",
        empty="NA",
    )
    if differential_required and len(differential) != len(expected_differential):
        public = _atom(
            "INCONCLUSIVE", source="candidate-public-contract-observations",
            evidence={"expected_differential": len(expected_differential), "observed_differential": len(differential)},
        )
    p2p_regression = _group_status(
        p2p,
        expected=len(expected_tests.get("p2p", []))
        if isinstance(expected_tests.get("p2p", []), Sequence)
        else None,
        source="candidate-p2p-observations",
        empty="NA",
    )
    state_applicable = bool(policy.get("state_applicable"))
    expected_state = expected_tests.get("state", [])
    expected_state = expected_state if isinstance(expected_state, list) else []
    state_roles = {
        str(item.get("id")): str(item.get("state_role", ""))
        for item in expected_state if isinstance(item, Mapping)
    }
    invalid_state_roles = state_applicable and (
        not state_roles
        or any(role not in {"critical_data", "required_state"} for role in state_roles.values())
    )
    critical_rows = [row for row in state if state_roles.get(str(row.get("id"))) == "critical_data"]
    expected_critical = sum(role == "critical_data" for role in state_roles.values())
    critical = (
        _atom("INCONCLUSIVE", source="state-role-validation", evidence={"missing_or_invalid_roles": sorted(state_roles)})
        if invalid_state_roles
        else _group_status(
            critical_rows,
            expected=expected_critical,
            source="candidate-critical-data-observations",
            empty="NA" if not state_applicable else "INCONCLUSIVE",
        )
    )
    anti_evidence = {
        "observation_errors": list(observation_errors),
        "protected_hits": protected_hits,
        "report_marker_hits": marker_hits,
        "receipt_count": len(receipts),
        "valid_receipt_count": len(valid_receipts),
    }
    cheating_errors = [
        error for error in observation_errors
        if any(
            marker in str(error).casefold()
            for marker in ("receipt", "duplicate", "unexpected", "forged")
        )
    ]
    return {
        "patch_applicable": _status(patch_applicable, source="candidate-patch"),
        "build_pass": build_evidence,
        "dependency_resolved": dependency_evidence,
        "no_deceptive_source_fallback": _atom(
            "FAIL" if deceptive_hits else "INCONCLUSIVE" if missing_l2 else "NA" if not deceptive_rules else "PASS",
            source="deceptive-fallback-scan",
            evidence=deceptive_hits,
        ),
        "public_contract_preserved": public,
        "migration_scope_preserved": _atom(
            "FAIL" if scope_policy_hits else "PASS",
            source="changed-path-allowlist",
            evidence=scope_policy_hits,
        ),
        "p2p_regression_preserved": p2p_regression,
        "critical_data_preserved": critical,
        "tests_not_tampered": tests_not_tampered,
        "no_new_skip_or_xfail": no_new_skip,
        "protected_paths_unchanged": _atom(
            "FAIL" if protected_hits else "PASS",
            source="protected-path-inventory",
            evidence=protected_hits,
        ),
        "anti_cheating_pass": _atom(
            "FAIL" if cheating_errors or protected_hits or marker_hits else "PASS",
            source="observation-and-report-integrity",
            evidence=anti_evidence,
        ),
        "source_hits": [*residue_hits, *deceptive_hits],
        "ordinary_residue_hits": residue_hits,
        "scope_policy_hits": scope_policy_hits,
        "deceptive_fallback_hits": deceptive_hits,
        "residue_diagnostics": residue_diagnostics,
        "deceptive_residue_diagnostics": deceptive_diagnostics,
        "cheating": bool(
            deceptive_hits
            or cheating_errors
            or protected_hits
            or marker_hits
        ),
        "evidence_mode": "derived",
    }


__all__ = ["build_hardgate_evidence", "build_source_test_baseline"]

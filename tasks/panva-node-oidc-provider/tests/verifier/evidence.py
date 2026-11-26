"""Evidence derivation for the fixed Migration Grader hardgate contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .residue import classify_residue_rules, scan_residue_rules, scan_unresolved_dynamic_references
except ImportError:  # Standalone Runtime Draft layout.
    from residue import classify_residue_rules, scan_residue_rules, scan_unresolved_dynamic_references


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
    return "test" in {part.casefold() for part in parts[:-1]} or bool(
        _TEST_NAME.search(path)
    )


_EXCLUDED_PARTS = frozenset(
    {".git", "node_modules", ".gomodcache", ".gocache", "__pycache__", ".pytest_cache"}
)


def _regular_files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        return {}
    values: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in _EXCLUDED_PARTS for part in Path(relative).parts):
            continue
        values[relative] = path
    return values


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


def _marker_counts(root: Path) -> dict[str, int]:
    counts = {"skip": 0, "xfail": 0, "disabled": 0}
    for relative, path in _regular_files(root).items():
        if not _is_test(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
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
            if path and (normalized == path or normalized.startswith(path + "/")):
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


_TEST_TITLE = re.compile(r"""\b(?:it|test|specify)\s*\(\s*['"`]([^'"`]+)['"`]""")
_DESCRIBE_TITLE = re.compile(r"""\b(?:describe|context)\s*\(\s*['"`]([^'"`]+)['"`]""")
_ASSERTION_CALL = re.compile(
    r"""\bassert\b|\.\s*expect\s*\(|\bexpect\s*\(|\.\s*should\b|\bshould\s*\("""
)


def _test_strength(text: str) -> dict[str, object]:
    """Summarize a test file's observable strength (cases, suites, assertions).

    Used to distinguish an equivalent wiring adaptation (mount the provider a
    different way, same cases and assertions) from a weakened test suite
    (a removed case or assertion). Prose is stripped so comments never count.
    """

    stripped = _strip_comments(text)
    return {
        "tests": sorted(set(_TEST_TITLE.findall(stripped))),
        "describes": sorted(set(_DESCRIBE_TITLE.findall(stripped))),
        "assertions": len(_ASSERTION_CALL.findall(stripped)),
    }


def _strength_weakened(before: Mapping[str, object], after: Mapping[str, object]) -> bool:
    if not set(before.get("tests", [])).issubset(set(after.get("tests", []))):
        return True
    if not set(before.get("describes", [])).issubset(set(after.get("describes", []))):
        return True
    return int(after.get("assertions", 0)) < int(before.get("assertions", 0))


def _compact_js(text: str) -> str:
    """Ignore formatting and comments, preserving every quoted value."""
    try:
        from .residue import _strip_comments as strip_js_comments
    except ImportError:
        from residue import _strip_comments as strip_js_comments
    return "".join(re.findall(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`|[^\s]", strip_js_comments(text)))


def _setup_projection(block: str) -> str:
    """Recognize only equivalent host wiring; assertions remain byte-for-byte
    equivalent after formatting normalization. Unknown setup code stays intact.
    """
    header = re.fullmatch(
        r"before\((?:async)?function\(\)\{this\.provider\.use\((?:async)?\(ctx,next\)=>\{(.*?)\}\);\}\);",
        block,
    )
    if header:
        statements = header[1]
        statements = re.sub(r"(?:return|await)next\(\);$", "", statements)
        calls = re.findall(r"ctx\.set\(((?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"),(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"))\);", statements)
        if calls and statements == "".join(f"ctx.set({args});" for args in calls):
            return "before(headers:" + "|".join(calls) + ");"
    for prefix in (
        "before(function(){this.requestHook((_req,res)=>{",
        "before(()=>{globalThis.server.prependListener('request',(_req,res)=>{",
    ):
        if block.startswith(prefix) and block.endswith("});});"):
            statements = block[len(prefix):-len("});});")]
            calls = re.findall(r"res\.setHeader\(((?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"),(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"))\);", statements)
            if calls and statements == "".join(f"res.setHeader({args});" for args in calls):
                return "before(headers:" + "|".join(calls) + ");"
    cors = re.fullmatch(r"before\((?:async)?function\(\)\{this\.provider\.use\(cors\(\{origin:((?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")),?\}\)\);\}\);", block)
    if cors:
        return f"before(cors-origin:{cors[1]});"
    origin = re.search(r"res\.setHeader\('access-control-allow-origin',('(?:\\.|[^'\\])*')\)", block)
    if origin:
        value = origin[1]
        # Equivalent translation of the upstream @koa/cors default preflight.
        projected = f"""before(function () {{
          this.requestHook((req, res) => {{
            res.setHeader('vary', 'Origin');
            if (req.method !== 'OPTIONS') {{
              res.setHeader('access-control-allow-origin', {value}); return;
            }}
            if (!req.headers['access-control-request-method']) return;
            res.setHeader('access-control-allow-origin', {value});
            res.setHeader('access-control-allow-methods', 'GET,HEAD,PUT,POST,DELETE,PATCH');
            if (req.headers['access-control-request-headers']) {{
              res.setHeader('access-control-allow-headers', req.headers['access-control-request-headers']);
            }}
            res.statusCode = 204;
          }});
        }});"""
        if block == _compact_js(projected):
            return f"before(cors-origin:{value});"
    origin = re.search(r"app\.use\(cors\(\{origin:('(?:\\.|[^'\\])*'),?\}\)\)", block)
    if origin:
        value = origin[1]
        # Keep the real CORS library on an external Koa host, adapting only
        # the provider mount to its required Node callback contract.
        projected = f"""before(async function () {{
          const app = new Koa();
          app.use(cors({{ origin: {value}, }}));
          const mountTo = process.env.MOUNT_VIA ? process.env.MOUNT_TO || '/' : '/';
          const {{ provider }} = this;
          app.use(async (ctx, next) => {{
            ctx.req.originalUrl = ctx.req.url;
            try {{ await next(); }} finally {{ delete ctx.req.originalUrl; }}
          }});
          app.use(koaMount(mountTo, async (ctx) => {{
            ctx.respond = false;
            await new Promise((resolve) => {{
              ctx.res.on('finish', resolve); ctx.res.on('close', resolve);
              provider.callback()(ctx.req, ctx.res);
            }});
          }}));
          globalThis.server.removeAllListeners('request');
          globalThis.server.on('request', app.callback());
        }});"""
        if block == _compact_js(projected):
            return f"before(cors-origin:{value});"
    return block


def _canonical_test_wiring(text: str) -> str:
    text = _compact_js(text).replace("this.use(", "this.provider.use(")
    # The proxy flag used to live on the Koa host. Setting it on the provider
    # keeps the same request and assertions; a descriptive title may follow it.
    text = text.replace("if(this.app){this.app.proxy=true;}else{this.provider.proxy=true;}", "this.provider.proxy=true;")
    text = re.sub(r"(['\"])is trusted when proxy=true (?:is set on the koa app|is set on the provider|is enabled on the provider)\1", "'proxy trust enabled'", text)
    # Locate complete setup callbacks, respecting quoted braces/parentheses.
    tokens = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`|.", re.S)
    spans = []
    for match in re.finditer(r"\bbefore\(", text):
        depth = 1
        for token in tokens.finditer(text, match.end()):
            value = token[0]
            if value == "(": depth += 1
            elif value == ")": depth -= 1
            if depth == 0:
                end = token.end() + (text[token.end():token.end() + 1] == ";")
                spans.append((match.start(), end))
                break
    projected_cors = False
    for start, end in reversed(spans):
        old = text[start:end]
        new = _setup_projection(old)
        projected_cors |= new.startswith("before(cors-origin:")
        text = text[:start] + new + text[end:]
    if projected_cors:
        # These imports are used only by the recognized external-host setup.
        for statement in ("import Koa from 'koa';", "import koaMount from 'koa-mount';", "import cors from '@koa/cors';"):
            text = text.replace(_compact_js(statement), "")
    return text


def _equivalent_test_wiring(before: str, after: str) -> bool:
    original, current = _canonical_test_wiring(before), _canonical_test_wiring(after)
    if original == current:
        return True
    # Extra Fastify startup time may require a larger finite Mocha timeout.
    # Preserve the call and every surrounding statement; never accept a lower
    # timeout, a removed timeout, or a change to a test/request/assertion.
    old = re.findall(r"\.timeout\((\d+)\)", original)
    new = re.findall(r"\.timeout\((\d+)\)", current)
    return bool(old) and len(old) == len(new) and all(int(a) <= int(b) <= 60000 for a, b in zip(old, new)) and re.sub(r"\.timeout\(\d+\)", ".timeout(FINITE)", original) == re.sub(r"\.timeout\(\d+\)", ".timeout(FINITE)", current)


def _test_integrity(
    root: Path,
    baseline: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    *,
    source: str = "source-test-baseline",
    pristine_root: Path | None = None,
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
    )
    # The instruction permits equivalent public mounting adaptations. Inspect
    # the actual transformation instead of treating a finite filename allowlist
    # as an exhaustive definition of permitted wiring.
    offending = []
    weakened: list[str] = []
    for relative in changed:
        if pristine_root is not None:
            pristine_path = Path(pristine_root) / relative
            if pristine_path.is_symlink() or not pristine_path.is_file():
                weakened.append(relative)
                continue
            try:
                before_text = pristine_path.read_text(encoding="utf-8", errors="ignore")
                after_text = current[relative].read_text(encoding="utf-8", errors="ignore")
                if _equivalent_test_wiring(before_text, after_text):
                    continue
                if relative not in allowed:
                    offending.append(relative)
                    continue
                before = _test_strength(before_text)
                after = _test_strength(after_text)
            except OSError:
                weakened.append(relative)
                continue
            if _strength_weakened(before, after):
                weakened.append(relative)
        elif relative not in allowed:
            offending.append(relative)
    marker_before = baseline.get("marker_counts", {})
    marker_before = marker_before if isinstance(marker_before, Mapping) else {}
    marker_after = _marker_counts(root)
    marker_delta = {
        key: int(marker_after.get(key, 0)) - int(marker_before.get(key, 0))
        for key in {"skip", "xfail", "disabled"}
    }
    tamper = _atom(
        "FAIL" if missing or offending or weakened else "PASS",
        source=source,
        evidence={
            "missing": missing,
            "changed": changed,
            "offending": offending,
            "weakened": weakened,
        },
    )
    skip = _atom(
        "FAIL" if any(value > 0 for value in marker_delta.values()) else "PASS",
        source=source,
        evidence={"before": dict(marker_before), "after": marker_after, "delta": marker_delta},
    )
    return tamper, skip


def _partition_inherited_source_hits(
    candidate_root: Path,
    pristine_root: Path | None,
    hits: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep unchanged starting-source residue separate from deceptive fallback.

    An empty or incomplete migration is still rejected by scope/F2P checks.
    Merely retaining the source framework in an unchanged file is not evidence
    that the candidate introduced a deceptive fallback. Require byte-identical
    regular files in the trusted pristine tree; changed/new files and missing
    baseline evidence retain the existing fallback checks.
    """
    active: list[dict[str, Any]] = []
    inherited: list[dict[str, Any]] = []
    unchanged: dict[str, bool] = {}
    for hit in hits:
        relative = str(hit.get("path", ""))
        if relative not in unchanged:
            path = Path(relative)
            same = False
            if (pristine_root is not None
                    and pristine_root.resolve() != candidate_root.resolve()
                    and relative and not path.is_absolute()
                    and ".." not in path.parts and "\\" not in relative):
                current = candidate_root / path
                original = pristine_root / path
                safe = all(
                    not (root / Path(*path.parts[:i])).is_symlink()
                    for root in (candidate_root, pristine_root)
                    for i in range(len(path.parts) + 1)
                )
                if safe and current.is_file() and original.is_file():
                    try:
                        same = current.read_bytes() == original.read_bytes()
                    except OSError:
                        pass
            unchanged[relative] = same
        (inherited if unchanged[relative] else active).append(dict(hit))
    return active, inherited


def _request_router_match_hits(candidate_root: Path) -> list[dict[str, object]]:
    """Find a second router deciding which live Fastify request is admitted.

    The Koa-typed middleware surface in helpers/router.js is outside the
    migration scope. A call with a Fastify request's method in the migrated
    entrypoint instead makes the legacy router a request-path gate.
    """
    try:
        from .residue import _strip_comments as strip_js_comments
    except ImportError:
        from residue import _strip_comments as strip_js_comments

    def without_strings(source: str) -> str:
        quoted = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`")
        return "\n".join(
            quoted.sub(lambda match: " " * len(match.group()), line)
            for line in source.split("\n")
        )

    pattern = re.compile(
        r"\brouter\s*\.\s*match\s*\(\s*(?:request|req)\s*\.\s*method\b"
    )
    hits: list[dict[str, object]] = []
    for relative in ("lib/provider.js", "lib/helpers/initialize_app.js"):
        path = candidate_root / relative
        if path.is_symlink() or not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        uncommented = without_strings(strip_js_comments(source))
        for match in pattern.finditer(uncommented):
            line = source.count("\n", 0, match.start()) + 1
            hits.append({
                "path": relative,
                "line": line,
                "token": source.splitlines()[line - 1].strip()[:160],
                "kind": "request-route-match",
                "rule_id": "fastify-route-ownership",
            })
    return hits


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
    pristine_root: Path | None = None,
    runtime_source_hits: Sequence[Mapping[str, Any]] = (),
) -> dict[str, object]:
    """Derive hardgate atoms from Case evidence instead of policy booleans."""

    policy = config.get("policy", {})
    policy = policy if isinstance(policy, Mapping) else {}
    rows = _rows(observations)
    expected_tests = config.get("tests", {})
    expected_tests = expected_tests if isinstance(expected_tests, Mapping) else {}
    p2p = [row for row in rows if row.get("kind") == "P2P"]
    state = [row for row in rows if row.get("kind") == "STATE"]
    semantic_rules = policy.get("semantic_rules", [])
    residue_rules = policy.get("ordinary_residue_rules", policy.get("residue_rules", semantic_rules))
    residue_diagnostics = classify_residue_rules(candidate_root, residue_rules)
    residue_hits = scan_residue_rules(candidate_root, residue_rules)
    residue_hits.extend(_request_router_match_hits(candidate_root))
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
    dynamic_hits = scan_unresolved_dynamic_references(candidate_root, deceptive_rules)
    runtime_hits = [dict(row) for row in (runtime_source_hits or [])]
    deceptive_hits, inherited_static = _partition_inherited_source_hits(
        candidate_root, pristine_root, deceptive_hits,
    )
    dynamic_hits, inherited_dynamic = _partition_inherited_source_hits(
        candidate_root, pristine_root, dynamic_hits,
    )
    runtime_hits, inherited_runtime = _partition_inherited_source_hits(
        candidate_root, pristine_root, runtime_hits,
    )
    inherited_hits = [*inherited_static, *inherited_dynamic, *inherited_runtime]
    # Preserve rejection and scope attribution even when a source reference was
    # found only by the runtime/dynamic scanner, rather than an ordinary rule.
    residue_hits.extend(inherited_hits)
    protected = policy.get("protected_paths", config.get("protected_paths", []))
    protected = protected if isinstance(protected, Sequence) and not isinstance(protected, (str, bytes)) else []
    marker_names = policy.get("forbidden_report_markers", [])
    marker_names = marker_names if isinstance(marker_names, Sequence) and not isinstance(marker_names, (str, bytes)) else []
    protected_hits = _protected_hits(candidate_root, [str(item) for item in protected])
    marker_hits = _report_marker_hits(candidate_root, [str(item) for item in marker_names])
    tests_not_tampered, no_new_skip = _test_integrity(
        candidate_root, source_test_baseline, config, source=test_baseline_source,
        pristine_root=pristine_root,
    )
    build_evidence, dependency_evidence, valid_receipts = _derive_execution_atoms(
        receipts
    )
    public = _group_status(
        p2p,
        expected=len(expected_tests.get("p2p", [])) if isinstance(expected_tests.get("p2p", []), Sequence) else None,
        source="candidate-observations",
        empty="NA",
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
            "FAIL" if (deceptive_hits or dynamic_hits or runtime_hits) else "INCONCLUSIVE" if missing_l2 else "NA" if not deceptive_rules else "PASS",
            source="deceptive-fallback-scan",
            evidence={"static": deceptive_hits, "dynamic": dynamic_hits, "runtime": runtime_hits},
        ),
        "public_contract_preserved": public,
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
        "source_hits": [*residue_hits, *deceptive_hits, *dynamic_hits, *runtime_hits],
        "ordinary_residue_hits": residue_hits,
        "deceptive_fallback_hits": deceptive_hits,
        "dynamic_fallback_hits": dynamic_hits,
        "runtime_fallback_hits": runtime_hits,
        "inherited_source_hits": inherited_hits,
        "residue_diagnostics": residue_diagnostics,
        "deceptive_residue_diagnostics": deceptive_diagnostics,
        "cheating": bool(
            deceptive_hits
            or dynamic_hits
            or runtime_hits
            or cheating_errors
            or protected_hits
            or marker_hits
        ),
        "evidence_mode": "derived",
    }


__all__ = ["build_hardgate_evidence", "build_source_test_baseline"]

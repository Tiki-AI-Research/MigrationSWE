from __future__ import annotations

import json
import re
import shlex
import tarfile
import hashlib
from pathlib import Path
from typing import Any

from sandbox import run_candidate_command


def evaluate_smoke_execution(repo: Path) -> dict[str, Any]:
    """Verify real app use and response assertions after target dependencies exist.

    The normal run must inject into and close the actual buildApp instance.
    A second independent run corrupts injected responses; a useful smoke must
    then fail an assertion. Neither run receives evaluator filesystem access.
    """
    probe = Path(__file__).with_name("smoke_execution_probe.js").read_text(encoding="utf-8")
    runs: list[dict[str, Any]] = []
    for mutate in (False, True):
        result = run_candidate_command(
            repo, ["node", "-"], timeout=30,
            stdin=f"const mutate = {str(mutate).lower()};\n" + probe,
        )
        evidence = None
        lines = [line for line in result.get("stdout", "").splitlines() if line.startswith("HARBOR_SMOKE_EXECUTION=")]
        if len(lines) == 1:
            try:
                evidence = json.loads(lines[0].split("=", 1)[1])
            except json.JSONDecodeError:
                pass
        runs.append({"mutated_response": mutate, "execution": result, "observed": evidence})
        if result["status"] == "infrastructure_error":
            return {"id": "fastify_smoke_execution", "status": "infrastructure_error", "success": None, "runs": runs}
        observed = evidence if isinstance(evidence, dict) else {}
        if not mutate:
            valid = (
                result["status"] == "passed"
                and observed.get("builds", 0) > 0
                and observed.get("injections", 0) > 0
                and observed.get("responseReads", 0) > 0
                and observed.get("closes", 0) == observed.get("instances", -1)
                and observed.get("closes", 0) > 0
            )
        else:
            valid = (
                result["status"] == "candidate_failure"
                and not result.get("timed_out")
                and observed.get("builds", 0) > 0
                and observed.get("injections", 0) > 0
                and observed.get("responseReads", 0) > 0
            )
        if not valid:
            return {
                "id": "fastify_smoke_execution", "status": "candidate_failure", "success": False,
                "reason": "smoke must exercise the real bin/app.js buildApp/inject/close and fail on corrupted responses",
                "runs": runs,
            }
    return {"id": "fastify_smoke_execution", "status": "passed", "success": True, "runs": runs}

# Match test call sites in test/test.js:
# test('...', ...) or asyncTest('...', ...)
_TEST_CALL_RE = re.compile(r"\b(?:asyncTest|test)\s*\(", re.MULTILINE)
_TEST_NAME_RE = re.compile(r"\b(?:asyncTest|test)\s*\(\s*['\"]([^'\"]+)['\"]")
_ASSERTION_RE = re.compile(r"\bassert(?:\.[A-Za-z_$][\w$]*)?\s*\(")
_BUILD_APP_RE = re.compile(r"\bbuildApp\s*\(")
_INJECT_RE = re.compile(r"(?:\.|\b)inject\s*\(")
_CLOSE_RE = re.compile(r"(?:\.|\b)close\s*\(")
_SUCCESS_EXIT_RE = re.compile(r"\bprocess\s*\.\s*exit\s*\(\s*0?\s*\)")
# Runner flags that filter or exclude tests from an npm script segment.
# Applying one of them to the evaluator-owned base suite is tampering; applying
# one only to the candidate-owned smoke test is an ordinary candidate failure.
_SCRIPT_FILTER_RE = re.compile(
    r"(?<![\w-])(?:--grep|--testNamePattern|--test-name-pattern|--exclude|--ignore|--filter)(?==|\s|$)",
    re.IGNORECASE,
)
_SCRIPT_SEGMENT_RE = re.compile(r"(?:&&|\|\||;|\|)")
_RUNNER_NAMES = frozenset({"node", "nodejs", "npm", "npx", "mocha", "jest", "tape", "ava"})
_SMOKE_TEST_PATH = "test/fastify-smoke.js"
_BASE_TEST_PATH = "test/test.js"


def _success_exits_follow_smoke_evidence(content: str) -> bool:
    """Allow a successful exit only after the required smoke work appears.

    Some standalone Node smoke tests explicitly terminate in their completion
    callback.  That is not an escape hatch when buildApp, inject, assertions,
    and close all precede the exit.  An exit before any required lifecycle
    evidence remains a failure; the execution probe later verifies that the
    apparent evidence actually runs and observes corrupted responses.
    """
    # Assertions may use a custom helper rather than Node's assert module.
    # The paired execution probe verifies that bad responses cause failure.
    required = (_BUILD_APP_RE, _INJECT_RE, _CLOSE_RE)
    for exit_match in _SUCCESS_EXIT_RE.finditer(content):
        prefix = content[:exit_match.start()]
        if not all(pattern.search(prefix) for pattern in required):
            return False
    return True

# Match newly introduced skip/xfail/only tokens
_SKIP_ONLY_RE = re.compile(
    r"\b(?:\.skip|\.only|xit|xdescribe|it\.skip|test\.skip|describe\.skip)\b",
    re.IGNORECASE,
)
_BASE_PLATFORM_SNIPPET = "const trickyNames = ['100%done.txt', 'a&b.txt', 'what?.txt', 'café.txt'];"
_APPROVED_PLATFORM_SNIPPET = (
    "const trickyNames = process.platform === 'win32'\n"
    "        ? ['100%done.txt', 'a&b.txt', 'café.txt']\n"
    "        : ['100%done.txt', 'a&b.txt', 'what?.txt', 'café.txt'];"
)


# The single permitted platform-compatibility adaptation. Any semantically
# equivalent formatting (single/multi-line, quote style, whitespace) must
# normalize to _BASE_PLATFORM_SNIPPET; anything else stays byte-identical.
_WIN32_LIST = ["100%done.txt", "a&b.txt", "café.txt"]
_OTHER_LIST = ["100%done.txt", "a&b.txt", "what?.txt", "café.txt"]
_PLATFORM_BRANCH_RE = re.compile(
    r"const\s+trickyNames\s*=\s*process\.platform\s*===\s*['\"]win32['\"]\s*\?\s*"
    r"\[([^\]]*)\]\s*:\s*\[([^\]]*)\]\s*;",
    re.S,
)
_PLATFORM_SPLICE_RE = re.compile(
    r"const\s+trickyNames\s*=\s*\[([^\]]*)\]\s*;\s*"
    r"(?:\/\/[^\n]*\n\s*)*"
    r"if\s*\(\s*process\.platform\s*===\s*['\"]win32['\"]\s*\)\s*"
    r"trickyNames\.splice\(\s*trickyNames\.indexOf\(\s*['\"]what\?\.txt['\"]\s*\)\s*,\s*1\s*\)\s*;",
    re.S,
)


def _parse_names(segment: str) -> list[str]:
    return [item.strip().strip("'\"") for item in segment.split(",") if item.strip()]


def _normalize_allowed_test_migration(content: str) -> str:
    """Normalize only the frozen Windows filename compatibility adaptation.

    Matches the whole `const trickyNames = process.platform === 'win32' ?
    [...] : [...];` statement regardless of line breaks, indentation, or quote
    style. Only the exact frozen filename lists collapse to
    `_BASE_PLATFORM_SNIPPET`; any other list content is left untouched so it
    still fails the byte-for-byte comparison.
    """

    def repl(match: re.Match[str]) -> str:
        if (
            _parse_names(match.group(1)) == _WIN32_LIST
            and _parse_names(match.group(2)) == _OTHER_LIST
        ):
            return _BASE_PLATFORM_SNIPPET
        return match.group(0)

    normalized = _PLATFORM_BRANCH_RE.sub(repl, content)
    normalized = _PLATFORM_SPLICE_RE.sub(
        lambda match: _BASE_PLATFORM_SNIPPET
        if _parse_names(match.group(1)) == _OTHER_LIST else match.group(0),
        normalized,
    )
    # Documentation immediately attached to this one allowed adaptation does
    # not weaken any test. All code outside it still compares byte-for-byte.
    return re.sub(
        r"(?m)(?:^[ \t]*//[^\n]*\n)+(?=[ \t]*" + re.escape(_BASE_PLATFORM_SNIPPET) + r")",
        "",
        normalized,
    )


def _count_test_calls(content: str) -> int:
    return len(_TEST_CALL_RE.findall(content))


def _test_names(content: str) -> set[str]:
    return {match.group(1) for match in _TEST_NAME_RE.finditer(content)}


def _count_assertions(content: str) -> int:
    return len(_ASSERTION_RE.findall(content))


def _script_segments(script: str) -> list[str]:
    """Split an npm script into individually executable command segments."""
    return [segment.strip() for segment in _SCRIPT_SEGMENT_RE.split(script) if segment.strip()]


def _strip_env_assignments(tokens: list[str]) -> list[str]:
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens = tokens[1:]
    return tokens


def _segment_tokens(segment: str) -> list[str]:
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        tokens = segment.split()
    return _strip_env_assignments(tokens)


def _segment_test_targets(segment: str) -> set[str]:
    """Return the known test entrypoints a script segment actually invokes.

    A segment qualifies only when it starts with a test runner and a token
    resolves to the test file path, so a mention inside an unrelated argument
    (``echo test/fastify-smoke.js``) cannot satisfy the wiring requirement.
    """
    tokens = _segment_tokens(segment)
    if not tokens or Path(tokens[0]).name not in _RUNNER_NAMES:
        return set()
    targets: set[str] = set()
    for token in tokens:
        normalized = token.strip("\"'").replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized in (_BASE_TEST_PATH, _SMOKE_TEST_PATH):
            targets.add(normalized)
    return targets


def _segment_npm_script_ref(segment: str) -> str | None:
    """Return a locally defined npm script referenced by this segment."""
    tokens = _segment_tokens(segment)
    if not tokens or Path(tokens[0]).name not in {"npm", "npm.cmd"}:
        return None
    rest = tokens[1:]
    if rest[:1] == ["test"]:
        return "test"
    for index, token in enumerate(rest):
        if token in {"run", "run-script"}:
            for candidate in rest[index + 1:]:
                if not candidate.startswith("-"):
                    return candidate
            return None
    return None


def _script_targets_and_filters(
    scripts: dict[str, Any], name: str, seen: set[str]
) -> tuple[set[str], set[str]]:
    """Resolve the test targets and filtered targets of one npm script.

    Local ``npm run <script>`` references are followed (bounded by ``seen``) so
    wrapping ``node test/test.js`` in another script still satisfies wiring.
    """
    targets: set[str] = set()
    filtered: set[str] = set()
    script = scripts.get(name)
    if not isinstance(script, str) or name in seen:
        return targets, filtered
    seen = {*seen, name}
    for segment in _script_segments(script):
        segment_targets = _segment_test_targets(segment)
        targets.update(segment_targets)
        if segment_targets and _SCRIPT_FILTER_RE.search(segment):
            filtered.update(segment_targets)
        reference = _segment_npm_script_ref(segment)
        if reference:
            sub_targets, sub_filtered = _script_targets_and_filters(scripts, reference, seen)
            targets.update(sub_targets)
            filtered.update(sub_filtered)
    return targets, filtered


def _strip_js_noise(text: str) -> str:
    """Remove JS comments and string/template literals in a single pass.

    Requirement tokens must appear in executable code, so anything inside a
    ``//`` line comment, a ``/* ... */`` block comment, or a quoted literal
    (single, double, or template) is dropped.  Unterminated input is
    truncated instead of raising.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        following = text[index + 1] if index + 1 < length else ""
        if char == "/" and following == "/":
            index += 2
            while index < length and text[index] not in "\r\n":
                index += 1
        elif char == "/" and following == "*":
            index += 2
            while index < length and not (
                text[index] == "*" and index + 1 < length and text[index + 1] == "/"
            ):
                index += 1
            index = min(index + 2, length)
        elif char in "'\"`":
            quote = char
            index += 1
            while index < length:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    break
                index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _get_base_test_content(base_dir: Path | None, archive_path: Path | None) -> str | None:
    # 1. Try /opt/sharing-base/test/test.js
    if base_dir:
        candidate_file = base_dir / "test" / "test.js"
        if candidate_file.is_file():
            try:
                return candidate_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass

    # 2. Try frozen base archive at /tests/assets/base-repo.tar.gz
    if archive_path and archive_path.is_file():
        try:
            with tarfile.open(archive_path, "r:*") as tar:
                for member in tar.getmembers():
                    # Handle paths like test/test.js, ./test/test.js, or <prefix>/test/test.js
                    parts = Path(member.name).parts
                    if len(parts) >= 2 and parts[-2] == "test" and parts[-1] == "test.js":
                        f = tar.extractfile(member)
                        if f is not None:
                            return f.read().decode("utf-8")
        except (OSError, tarfile.TarError, UnicodeDecodeError):
            pass

    return None


def evaluate_test_integrity(
    repo: Path,
    *,
    base_dir: Path | None = None,
    archive_path: Path | None = None,
) -> dict[str, Any]:
    """Verify test integrity for sharing-express-to-fastify.

    Rules from INTERFACES.md:
    - Base test file `test/test.js` still exists.
    - Count of `test(` + `asyncTest(` call sites in candidate `test/test.js` >= base count.
    - Base counts read from `/opt/sharing-base/test/test.js` if present, else frozen base archive.
    - No newly introduced `.skip` / `.only` / `xit` / `xdescribe` / `it.skip` / `test.skip` / `describe.skip`.
    - No base test file deleted (`test/test.js`).
    - `test/fastify-smoke.js` is allowed (new file).
    - Deleting/weakening the frozen base tests => policy_violation.
    - A missing, incomplete, or skipped candidate-owned smoke test => candidate_failure.
    """
    repo = Path(repo).resolve()
    candidate_test_file = repo / "test" / "test.js"

    policy_reasons: list[str] = []
    candidate_reasons: list[str] = []
    checks: list[dict[str, Any]] = []

    # 1. Check existence of candidate test/test.js
    if not candidate_test_file.is_file():
        policy_reasons.append("tests_modified")
        checks.append({
            "id": "base_test_file_exists",
            "status": "policy_violation",
            "reason": "base test file test/test.js was deleted or moved",
        })
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "test_integrity",
            "applicable": True,
            "success": False,
            "status": "policy_violation",
            "summary": "base test file test/test.js is missing",
            "policy_reasons": policy_reasons,
            "reasons": policy_reasons,
            "checks": checks,
        }

    checks.append({
        "id": "base_test_file_exists",
        "status": "passed",
        "path": "test/test.js",
    })

    try:
        candidate_content = candidate_test_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "test_integrity",
            "applicable": True,
            "success": False,
            "status": "candidate_failure",
            "summary": f"unable to read candidate test/test.js: {exc}",
            "checks": checks,
        }

    smoke_test_file = repo / "test" / "fastify-smoke.js"
    if not smoke_test_file.is_file():
        candidate_reasons.append("required_fastify_smoke_missing")
        checks.append({
            "id": "fastify_smoke_test",
            "status": "candidate_failure",
            "reason": "required test/fastify-smoke.js is missing",
        })
    else:
        try:
            smoke_content = smoke_test_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return {
                "schema_version": "harbor-migration-check/v1",
                "id": "test_integrity",
                "applicable": True,
                "success": False,
                "status": "candidate_failure",
                "summary": f"unable to read candidate test/fastify-smoke.js: {exc}",
                "checks": checks,
            }

        stripped_smoke_content = _strip_js_noise(smoke_content)
        smoke_requirements = {
            "non_empty": bool(stripped_smoke_content.strip()),
            "assertion": bool(_ASSERTION_RE.search(stripped_smoke_content)),
            "build_app": bool(_BUILD_APP_RE.search(stripped_smoke_content)),
            "inject": bool(_INJECT_RE.search(stripped_smoke_content)),
            "close": bool(_CLOSE_RE.search(stripped_smoke_content)),
            "no_success_exit": _success_exits_follow_smoke_evidence(stripped_smoke_content),
        }
        # A literal assert() call is diagnostic only: custom assertions are
        # valid when the execution probe detects failure on corrupted output.
        required_smoke = {key: value for key, value in smoke_requirements.items() if key != "assertion"}
        if not all(required_smoke.values()):
            candidate_reasons.append("required_fastify_smoke_incomplete")
            checks.append({
                "id": "fastify_smoke_test",
                "status": "candidate_failure",
                "requirements": smoke_requirements,
                "reason": "Fastify smoke test must exercise buildApp and inject with an assertion, close the instance, and not exit successfully before doing so",
            })
        else:
            checks.append({
                "id": "fastify_smoke_test",
                "status": "passed",
                "path": "test/fastify-smoke.js",
            })

    # 2. Get base test content and compare test call sites, names, and asserts.
    base_content = _get_base_test_content(base_dir, archive_path)
    if base_content is None:
        # Configuration error or infrastructure error: cannot locate base test reference
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "test_integrity",
            "applicable": True,
            "success": None,
            "status": "configuration_error",
            "summary": "base test reference not found in base_dir or archive_path",
            "checks": checks,
        }

    base_call_count = _count_test_calls(base_content)
    candidate_call_count = _count_test_calls(candidate_content)
    base_names = _test_names(base_content)
    candidate_names = _test_names(candidate_content)
    base_assertions = _count_assertions(base_content)
    candidate_assertions = _count_assertions(candidate_content)

    normalized_base = _normalize_allowed_test_migration(base_content)
    normalized_candidate = _normalize_allowed_test_migration(candidate_content)
    if normalized_candidate != normalized_base:
        policy_reasons.append("tests_modified")
        checks.append({
            "id": "test_content_allowlist",
            "status": "policy_violation",
            "reason": "test/test.js contains changes outside the frozen platform-compatibility allowlist",
            "base_sha256": hashlib.sha256(normalized_base.encode("utf-8")).hexdigest(),
            "candidate_sha256": hashlib.sha256(normalized_candidate.encode("utf-8")).hexdigest(),
        })
    else:
        checks.append({
            "id": "test_content_allowlist",
            "status": "passed",
            "normalized_sha256": hashlib.sha256(normalized_candidate.encode("utf-8")).hexdigest(),
        })

    if candidate_call_count < base_call_count:
        policy_reasons.append("tests_modified")
        checks.append({
            "id": "test_call_sites_count",
            "status": "policy_violation",
            "base_count": base_call_count,
            "candidate_count": candidate_call_count,
            "reason": f"test call site count decreased from {base_call_count} to {candidate_call_count}",
        })
    else:
        checks.append({
            "id": "test_call_sites_count",
            "status": "passed",
            "base_count": base_call_count,
            "candidate_count": candidate_call_count,
        })

    missing_names = sorted(base_names - candidate_names)
    if missing_names:
        policy_reasons.append("tests_modified")
        checks.append({
            "id": "test_names_preserved",
            "status": "policy_violation",
            "missing": missing_names,
            "reason": "candidate removed one or more named base tests",
        })
    else:
        checks.append({
            "id": "test_names_preserved",
            "status": "passed",
            "base_count": len(base_names),
            "candidate_count": len(candidate_names),
        })

    if candidate_assertions < base_assertions:
        policy_reasons.append("tests_modified")
        checks.append({
            "id": "assertions_preserved",
            "status": "policy_violation",
            "base_count": base_assertions,
            "candidate_count": candidate_assertions,
            "reason": "candidate removed assertion call sites from the base suite",
        })
    else:
        checks.append({
            "id": "assertions_preserved",
            "status": "passed",
            "base_count": base_assertions,
            "candidate_count": candidate_assertions,
        })

    # 3. Check for newly introduced skip/only tokens in test files
    # Base test files might already have some tokens (or not); verify no new skips
    base_skip_matches = set(_SKIP_ONLY_RE.findall(base_content))

    base_skip_hits: list[dict[str, Any]] = []
    candidate_skip_hits: list[dict[str, Any]] = []
    unreadable_tests: list[dict[str, str]] = []
    test_dir = repo / "test"
    if test_dir.is_dir():
        for test_path in sorted(test_dir.rglob("*")):
            if test_path.is_file() and test_path.suffix.lower() in {".js", ".mjs", ".ts"}:
                try:
                    text = test_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    unreadable_tests.append({
                        "path": test_path.relative_to(repo).as_posix(),
                        "error": str(exc),
                    })
                    continue
                matches = _SKIP_ONLY_RE.findall(text)
                if matches:
                    rel_path = test_path.relative_to(repo).as_posix()
                    # If this is test/test.js, check whether new tokens appeared
                    if rel_path == "test/test.js":
                        new_matches = [m for m in matches if m not in base_skip_matches]
                        if new_matches:
                            base_skip_hits.append({"path": rel_path, "tokens": new_matches})
                    else:
                        # Candidate-owned tests must run, but skipping them is
                        # an ordinary candidate failure, not base-test tampering.
                        candidate_skip_hits.append({"path": rel_path, "tokens": matches})

    if unreadable_tests:
        checks.append({
            "id": "test_files_readable_utf8",
            "status": "candidate_failure",
            "files": unreadable_tests,
            "reason": "one or more candidate test files are unreadable or not valid UTF-8",
        })
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "test_integrity",
            "applicable": True,
            "success": False,
            "status": "candidate_failure",
            "summary": "candidate test files must be readable UTF-8",
            "checks": checks,
        }

    checks.append({
        "id": "test_files_readable_utf8",
        "status": "passed",
    })

    if base_skip_hits:
        policy_reasons.append("new_skip_or_xfail")
    if candidate_skip_hits:
        candidate_reasons.append("new_skip_or_xfail")
    if base_skip_hits or candidate_skip_hits:
        checks.append({
            "id": "no_new_skip_or_only",
            "status": "policy_violation" if base_skip_hits else "candidate_failure",
            "hits": [*base_skip_hits, *candidate_skip_hits],
            "reason": "newly introduced skip or only tokens detected",
        })
    else:
        checks.append({
            "id": "no_new_skip_or_only",
            "status": "passed",
        })

    package_path = repo / "package.json"
    test_script = ""
    test_script_targets: set[str] = set()
    filtered_targets: set[str] = set()
    if package_path.is_file():
        try:
            package_text = package_path.read_text(encoding="utf-8")
            package_data = json.loads(package_text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {
                "schema_version": "harbor-migration-check/v1",
                "id": "test_integrity",
                "applicable": True,
                "success": False,
                "status": "candidate_failure",
                "summary": f"unable to read package.json for test selection audit: {exc}",
                "checks": checks,
            }
        scripts = package_data.get("scripts") if isinstance(package_data, dict) else None
        if not isinstance(scripts, dict):
            scripts = {}
        raw_script = scripts.get("test")
        test_script = raw_script if isinstance(raw_script, str) else ""
        test_script_targets, filtered_targets = _script_targets_and_filters(scripts, "test", set())

    base_selection_filtered = _BASE_TEST_PATH in filtered_targets
    smoke_selection_filtered = _SMOKE_TEST_PATH in filtered_targets

    missing_wiring = [
        path for path in (_BASE_TEST_PATH, _SMOKE_TEST_PATH)
        if path not in test_script_targets
    ]
    if missing_wiring:
        candidate_reasons.append("required_test_not_wired")
        checks.append({
            "id": "npm_test_wiring",
            "status": "candidate_failure",
            "missing": missing_wiring,
            "script": test_script,
            "reason": "package.json scripts.test must invoke both test/test.js and test/fastify-smoke.js",
        })
    else:
        checks.append({
            "id": "npm_test_wiring",
            "status": "passed",
            "targets": sorted(test_script_targets),
        })

    if base_selection_filtered:
        policy_reasons.append("tests_modified")
        checks.append({
            "id": "test_selection_unmodified",
            "status": "policy_violation",
            "hits": sorted(set(_SCRIPT_FILTER_RE.findall(test_script))),
            "reason": "test selection overrides were applied to the evaluator-owned base suite",
        })
    elif smoke_selection_filtered:
        candidate_reasons.append("test_selection_modified")
        checks.append({
            "id": "test_selection_unmodified",
            "status": "candidate_failure",
            "hits": sorted(set(_SCRIPT_FILTER_RE.findall(test_script))),
            "reason": "candidate-owned smoke test is filtered out by the test script",
        })
    else:
        checks.append({"id": "test_selection_unmodified", "status": "passed"})

    is_success = not policy_reasons and not candidate_reasons
    status = (
        "passed"
        if is_success
        else ("policy_violation" if policy_reasons else "candidate_failure")
    )
    reasons = [*policy_reasons, *candidate_reasons]
    return {
        "schema_version": "harbor-migration-check/v1",
        "id": "test_integrity",
        "applicable": True,
        "success": is_success,
        "status": status,
        "summary": (
            "test integrity preserved"
            if is_success
            else f"test integrity {status}: {', '.join(reasons)}"
        ),
        "policy_reasons": policy_reasons,
        "reasons": reasons,
        "base_call_count": base_call_count,
        "candidate_call_count": candidate_call_count,
        "base_assertion_count": base_assertions,
        "candidate_assertion_count": candidate_assertions,
        "checks": checks,
    }

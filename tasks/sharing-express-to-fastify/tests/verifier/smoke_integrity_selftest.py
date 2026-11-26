#!/usr/bin/env python3
"""Self-test for the ``test/fastify-smoke.js`` integrity requirement.

Runnable as a script (``python3 smoke_integrity_selftest.py``) or collected by
pytest (``test_*`` functions).  Standard library only.

Locks the rule that the required Fastify smoke test must contain real
executable calls -- ``buildApp``, ``inject``, an assertion, and ``close`` --
and that tokens appearing only inside comments or string/template literals do
not satisfy the requirement.
"""
from __future__ import annotations

import json
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_integrity import (  # noqa: E402
    _APPROVED_PLATFORM_SNIPPET,
    _BASE_PLATFORM_SNIPPET,
    _normalize_allowed_test_migration,
    _strip_js_noise,
    evaluate_test_integrity,
)

BASE_ARCHIVE = Path(__file__).resolve().parent.parent / "assets" / "base-repo.tar.gz"
WIRED_TEST_SCRIPT = "node test/test.js && node test/fastify-smoke.js"
UNWIRED_TEST_SCRIPT = "node test/test.js"

# Customer-reported no-op that used to pass: every requirement token lives in a
# single line comment, so the file contains no executable code at all.
CUSTOMER_POC = """\
// buildApp( inject( close( assert(
console.log('fastify smoke: no-op');
"""

# A file whose only content is comments must count as empty.
COMMENT_ONLY = """\
// buildApp( inject( close( assert(
/* buildApp( inject( close( assert( */
"""

# Every requirement token is present, but only inside a string literal.
STRING_LITERAL_POC = """\
// fastify smoke placeholder
const message = "buildApp( inject( close( assert(";
console.log(message);
"""

# Verbatim test/fastify-smoke.js from a real trial
# (jobs/sharing-express-to-fastify__eVbgM4k/artifacts/logs/artifacts/model.patch).
REAL_SMOKE_TEST = """\
/**
 * Focused Fastify lifecycle smoke test: buildApp() returns a real Fastify
 * instance, ready()/inject()/close() work, and a normal route responds.
 */

const assert = require('assert');
const { buildApp } = require('../bin/app');

(async () => {
    const app = buildApp({
        sharePath: __dirname,
        receive: false,
        clipboard: false,
        updateClipboardData: null,
        postUploadRedirectUrl: '',
        shareAddress: 'http://127.0.0.1/share/',
    });

    assert.strictEqual(typeof app.ready, 'function', 'buildApp() must expose ready()');
    assert.strictEqual(typeof app.inject, 'function', 'buildApp() must expose inject()');
    assert.strictEqual(typeof app.close, 'function', 'buildApp() must expose close()');

    await app.ready();

    const qr = await app.inject({ method: 'GET', url: '/qr' });
    assert.strictEqual(qr.statusCode, 200, 'GET /qr should succeed');
    assert.ok(qr.body.indexOf('Scan with your phone') !== -1, 'GET /qr should render the QR page');

    const missing = await app.inject({ method: 'GET', url: '/no-such-route' });
    assert.strictEqual(missing.statusCode, 404, 'unknown routes should 404');

    await app.close();
    console.log('fastify smoke: buildApp()/inject()/close() ok');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
"""

# The same real smoke test with its lifecycle teardown call removed.
REAL_SMOKE_TEST_WITHOUT_CLOSE = REAL_SMOKE_TEST.replace("await app.close();", "")


def _read_base_test_js() -> str:
    """Read test/test.js from the frozen base archive."""
    with tarfile.open(BASE_ARCHIVE, "r:*") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if len(parts) >= 2 and parts[-2] == "test" and parts[-1] == "test.js":
                handle = tar.extractfile(member)
                if handle is not None:
                    return handle.read().decode("utf-8")
    raise AssertionError(f"test/test.js not found in {BASE_ARCHIVE}")


def _build_repo(root: Path, smoke_content: str, test_script: str = WIRED_TEST_SCRIPT) -> Path:
    """Create a synthetic repo: frozen base test/test.js + candidate smoke test."""
    repo = root / "repo"
    test_dir = repo / "test"
    test_dir.mkdir(parents=True)
    (test_dir / "test.js").write_text(_read_base_test_js(), encoding="utf-8")
    (test_dir / "fastify-smoke.js").write_text(smoke_content, encoding="utf-8")
    (repo / "package.json").write_text(
        json.dumps({"scripts": {"test": test_script}}), encoding="utf-8"
    )
    return repo


def _evaluate(smoke_content: str, test_script: str = WIRED_TEST_SCRIPT) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _build_repo(Path(tmp), smoke_content, test_script)
        return evaluate_test_integrity(repo, archive_path=BASE_ARCHIVE)


def _smoke_check(result: dict[str, Any]) -> dict[str, Any]:
    return next(check for check in result["checks"] if check["id"] == "fastify_smoke_test")


def _check(result: dict[str, Any], check_id: str) -> dict[str, Any]:
    return next(check for check in result["checks"] if check["id"] == check_id)


def test_customer_comment_only_poc_is_rejected() -> None:
    result = _evaluate(CUSTOMER_POC)
    check = _smoke_check(result)
    assert check["status"] == "candidate_failure"
    assert check["requirements"]["assertion"] is False
    assert check["requirements"]["build_app"] is False
    assert check["requirements"]["inject"] is False
    assert check["requirements"]["close"] is False
    assert result["success"] is False
    assert result["status"] == "candidate_failure"
    assert "required_fastify_smoke_incomplete" in result["reasons"]


def test_comment_only_file_counts_as_empty() -> None:
    result = _evaluate(COMMENT_ONLY)
    check = _smoke_check(result)
    assert check["requirements"]["non_empty"] is False
    assert check["status"] == "candidate_failure"
    assert "required_fastify_smoke_incomplete" in result["reasons"]


def test_string_literal_only_tokens_are_rejected() -> None:
    result = _evaluate(STRING_LITERAL_POC)
    check = _smoke_check(result)
    assert check["status"] == "candidate_failure"
    assert check["requirements"]["non_empty"] is True
    assert check["requirements"]["assertion"] is False
    assert check["requirements"]["build_app"] is False
    assert check["requirements"]["inject"] is False
    assert check["requirements"]["close"] is False
    assert "required_fastify_smoke_incomplete" in result["reasons"]


def test_real_smoke_test_passes() -> None:
    result = _evaluate(REAL_SMOKE_TEST)
    check = _smoke_check(result)
    assert check["status"] == "passed"
    assert result["success"] is True
    assert result["status"] == "passed"
    assert "required_fastify_smoke_incomplete" not in result["reasons"]


def test_custom_assertion_is_deferred_to_execution_probe() -> None:
    custom = REAL_SMOKE_TEST.replace(
        "const assert = require('assert');",
        "function check(condition) { if (!condition) throw new Error('check failed'); }\n"
        "function checkEqual(actual, expected) { check(actual === expected); }",
    )
    custom = custom.replace("assert.strictEqual(", "checkEqual(").replace("assert.ok(", "check(")
    result = _evaluate(custom)
    assert "assert." not in custom
    assert _smoke_check(result)["status"] == "passed"
    assert result["status"] == "passed"


def test_documented_platform_branch_preserves_tests() -> None:
    base = _read_base_test_js()
    documented = base.replace(
        _BASE_PLATFORM_SNIPPET,
        "// Windows cannot create a question-mark filename.\n    " + _APPROVED_PLATFORM_SNIPPET,
    )
    assert _normalize_allowed_test_migration(documented) == _normalize_allowed_test_migration(base)


def test_guarded_platform_splice_preserves_tests() -> None:
    base = _read_base_test_js()
    guarded = base.replace(
        _BASE_PLATFORM_SNIPPET,
        _BASE_PLATFORM_SNIPPET + "\n    // Keep every other platform case.\n"
        "    if (process.platform === 'win32') trickyNames.splice(trickyNames.indexOf('what?.txt'), 1);",
    )
    assert _normalize_allowed_test_migration(guarded) == _normalize_allowed_test_migration(base)
    # Removing the filename on all platforms still changes the test suite.
    unguarded = guarded.replace("if (process.platform === 'win32') ", "")
    assert _normalize_allowed_test_migration(unguarded) != _normalize_allowed_test_migration(base)


def test_real_smoke_test_without_close_is_rejected() -> None:
    result = _evaluate(REAL_SMOKE_TEST_WITHOUT_CLOSE)
    check = _smoke_check(result)
    assert check["status"] == "candidate_failure"
    assert check["requirements"]["non_empty"] is True
    assert check["requirements"]["assertion"] is True
    assert check["requirements"]["build_app"] is True
    assert check["requirements"]["inject"] is True
    assert check["requirements"]["close"] is False
    assert "required_fastify_smoke_incomplete" in result["reasons"]


def test_successful_exit_before_smoke_is_rejected() -> None:
    result = _evaluate("process.exit(0);\n" + REAL_SMOKE_TEST)
    check = _smoke_check(result)
    assert check["status"] == "candidate_failure"
    assert check["requirements"]["no_success_exit"] is False
    assert result["status"] == "candidate_failure"


def test_implicit_successful_exit_before_smoke_is_rejected() -> None:
    result = _evaluate("process.exit();\n" + REAL_SMOKE_TEST)
    assert _smoke_check(result)["requirements"]["no_success_exit"] is False
    assert result["status"] == "candidate_failure"


def test_successful_exit_after_smoke_is_allowed() -> None:
    result = _evaluate(REAL_SMOKE_TEST + "\nPromise.resolve().then(() => process.exit(0));\n")
    check = _smoke_check(result)
    assert check["status"] == "passed"
    assert result["status"] == "passed"


def test_commented_successful_exit_does_not_block_smoke() -> None:
    result = _evaluate("// process.exit(0);\n" + REAL_SMOKE_TEST)
    assert _smoke_check(result)["status"] == "passed"
    assert result["status"] == "passed"


def test_smoke_must_be_wired_into_npm_test() -> None:
    result = _evaluate(REAL_SMOKE_TEST, UNWIRED_TEST_SCRIPT)
    wiring = _check(result, "npm_test_wiring")
    assert wiring["status"] == "candidate_failure"
    assert wiring["missing"] == ["test/fastify-smoke.js"]
    assert result["status"] == "candidate_failure"
    assert result["success"] is False
    assert "required_test_not_wired" in result["reasons"]


def test_wired_npm_test_invokes_both_suites() -> None:
    result = _evaluate(REAL_SMOKE_TEST, WIRED_TEST_SCRIPT)
    wiring = _check(result, "npm_test_wiring")
    assert wiring["status"] == "passed"
    assert set(wiring["targets"]) == {"test/test.js", "test/fastify-smoke.js"}
    assert result["status"] == "passed"
    assert result["success"] is True


def test_smoke_path_mentioned_outside_runner_does_not_satisfy_wiring() -> None:
    result = _evaluate(REAL_SMOKE_TEST, "node test/test.js && echo test/fastify-smoke.js")
    assert _check(result, "npm_test_wiring")["status"] == "candidate_failure"
    assert result["status"] == "candidate_failure"


def test_candidate_smoke_filter_is_ordinary_failure() -> None:
    result = _evaluate(
        REAL_SMOKE_TEST,
        "node test/test.js && node test/fastify-smoke.js --grep smoke",
    )
    selection = _check(result, "test_selection_unmodified")
    assert selection["status"] == "candidate_failure"
    assert result["status"] == "candidate_failure"
    assert result["policy_reasons"] == []


def test_base_suite_filter_is_policy_violation() -> None:
    result = _evaluate(
        REAL_SMOKE_TEST,
        "node test/test.js --grep share && node test/fastify-smoke.js",
    )
    selection = _check(result, "test_selection_unmodified")
    assert selection["status"] == "policy_violation"
    assert result["status"] == "policy_violation"
    assert result["policy_reasons"]


def test_unfiltered_wired_script_has_no_selection_violation() -> None:
    result = _evaluate(REAL_SMOKE_TEST, WIRED_TEST_SCRIPT)
    assert _check(result, "test_selection_unmodified")["status"] == "passed"


def test_strip_js_noise_tolerates_unterminated_input() -> None:
    for fragment in (
        "/* buildApp(",
        "// buildApp(",
        "const s = 'buildApp(",
        'const s = "buildApp(',
        "const s = `buildApp(",
    ):
        assert "buildApp" not in _strip_js_noise(fragment)


def test_strip_js_noise_ignores_markers_inside_strings() -> None:
    stripped = _strip_js_noise("const url = 'http://x/*y*/'; buildApp();")
    assert "http" not in stripped
    assert "buildApp(" in stripped


def _run_all() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, func in tests:
        try:
            func()
        except Exception as exc:  # any failure fails the self-test
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    print(f"{len(tests) - failures}/{len(tests)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())

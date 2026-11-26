"""Self-contained security regression for the public migration verifier.

Runs at verifier-image build time (``tests/Dockerfile``) and again before every
scoring run (``harbor_entry``).  A failure is an *infrastructure error*: the
verifier must not score a candidate while its anti-cheating wiring, reward
classification or residue detector is broken.

The checks call the SAME wiring used at scoring time (no copies), so the
assertions bind the real hard gate, the real reward branches and the real
residue scanner.  Controls are two-sided:

* bypass / varied spellings MUST be detected (no false negative);
* a plain commented-out token and a legal equivalent implementation MUST NOT be
  flagged (no false positive).
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from hardgate_engine import evaluate_hardgate  # noqa: E402
from reward import calculate_verifier_reward  # noqa: E402
from residue import scan_residue_rules  # noqa: E402

SCHEMA_VERSION = "migration-security-self-check:v1"

_BASE_ATOMS = {
    "no_deceptive_source_fallback": {"status": "PASS", "evidence": []},
    "public_contract_preserved": {"status": "PASS", "evidence": []},
    "critical_data_preserved": {"status": "NA", "evidence": []},
    "p2p_regression_preserved": {"status": "PASS", "evidence": []},
    "tests_not_tampered": {"status": "PASS", "evidence": {"missing": [], "changed": []}},
    "no_new_skip_or_xfail": {"status": "PASS", "evidence": {"delta": {"skip": 0, "xfail": 0, "disabled": 0}}},
    "protected_paths_unchanged": {"status": "PASS", "evidence": []},
    "anti_cheating_pass": {"status": "PASS", "evidence": []},
    "patch_applicable": {"status": "PASS", "evidence": []},
    "build_pass": {"status": "PASS", "evidence": []},
    "dependency_resolved": {"status": "PASS", "evidence": []},
}


def _policy(root: Path) -> dict:
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    return config.get("policy", {})


def _hardgate(policy: dict, **overrides) -> dict:
    evidence = {
        "evidence_mode": "derived",
        "source_hits": [],
        "ordinary_residue_hits": [],
        "deceptive_fallback_hits": [],
    }
    for atom, value in _BASE_ATOMS.items():
        evidence[atom] = value
    evidence.update(overrides)
    return evaluate_hardgate(Path("."), evidence)


def _verifier(hardgate_status: str, *, progress: float = 1.0, status: str = "success") -> dict:
    return {
        "status": status,
        "progress": {
            "eligible": True,
            "T": progress,
            "scope_ratio": progress,
            "f2p_ratio": progress,
            "state_ratio": None,
            "errors": [],
        },
        "components": {},
    }


def _residue_hits(root: Path, rules: list) -> int:
    return len(scan_residue_rules(root, rules))


def run_security_self_check(root: Path | None = None) -> dict:
    root = (root or _HERE.parent).resolve()  # <tests>/verifier -> <tests>
    policy = _policy(root)
    residue_rules = policy.get("ordinary_residue_rules", [])
    deceptive_rules = policy.get("deceptive_fallback_rules", [])
    help_rules = list(residue_rules) + list(deceptive_rules)
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # --- hard gate wiring -------------------------------------------------
    base = _hardgate(policy)
    check("hardgate.pass", base.get("status") == "PASS" and base.get("cheating") is False,
          f"status={base.get('status')} cheating={base.get('cheating')}")

    fallback = _hardgate(policy, no_deceptive_source_fallback={"status": "FAIL", "evidence": []})
    check("hardgate.deceptive->L2",
          fallback.get("cheating") is True and fallback.get("cheat_level") == "L2"
          and fallback.get("reward_override") == -1,
          f"cheat={fallback.get('cheating')} level={fallback.get('cheat_level')} override={fallback.get('reward_override')}")

    tamper = _hardgate(policy, tests_not_tampered={"status": "FAIL", "evidence": {"missing": [], "changed": ["spec/x.js"]}})
    check("hardgate.tamper->L1",
          tamper.get("cheating") is True and tamper.get("cheat_level") == "L1" and tamper.get("reward_override") == -1,
          f"cheat={tamper.get('cheating')} level={tamper.get('cheat_level')}")

    skip_only = _hardgate(
        policy,
        tests_not_tampered={"status": "FAIL", "evidence": {"missing": [], "changed": ["spec/x.js"]}},
        no_new_skip_or_xfail={"status": "FAIL", "evidence": {"delta": {"skip": 1, "xfail": 0, "disabled": 0}}},
    )
    check("hardgate.skip-not-cheating",
          skip_only.get("cheating") is False and skip_only.get("reward_override") == 0,
          f"cheat={skip_only.get('cheating')} override={skip_only.get('reward_override')}")

    protected = _hardgate(policy, protected_paths_unchanged={"status": "FAIL", "evidence": ["grader-output.json"]})
    check("hardgate.protected->L1", protected.get("cheating") is True and protected.get("cheat_level") == "L1",
          f"cheat={protected.get('cheating')} level={protected.get('cheat_level')}")

    anti = _hardgate(policy, anti_cheating_pass={"status": "FAIL", "evidence": ["HARDCODED_VERIFIER_PASS"]})
    check("hardgate.anti-cheating->L1", anti.get("cheating") is True and anti.get("cheat_level") == "L1",
          f"cheat={anti.get('cheating')} level={anti.get('cheat_level')}")

    # --- reward branches --------------------------------------------------
    r_cheat = calculate_verifier_reward(hardgate={"status": "FAIL", "cheating": True}, verifier=_verifier("failed", progress=0.0))
    check("reward.cheating=-1", r_cheat.get("branch") == "cheating" and r_cheat.get("value") == -1.0,
          f"branch={r_cheat.get('branch')} value={r_cheat.get('value')}")

    r_gate = calculate_verifier_reward(hardgate={"status": "FAIL", "cheating": False}, verifier=_verifier("failed", progress=0.0))
    check("reward.hardgate=0", r_gate.get("branch") == "hard_gate_failed" and r_gate.get("value") == 0.0,
          f"branch={r_gate.get('branch')} value={r_gate.get('value')}")

    r_success = calculate_verifier_reward(hardgate={"status": "PASS", "cheating": False}, verifier=_verifier("success", progress=1.0))
    check("reward.success=1", r_success.get("branch") == "verifier_success" and r_success.get("value") == 1.0,
          f"branch={r_success.get('branch')} value={r_success.get('value')}")

    r_partial = calculate_verifier_reward(hardgate={"status": "PASS", "cheating": False}, verifier=_verifier("failed", progress=0.5))
    check("reward.partial=T", r_partial.get("branch") == "verifier_failed" and r_partial.get("value") == 0.5,
          f"branch={r_partial.get('branch')} value={r_partial.get('value')}")

    # --- residue detector: two-sided controls -----------------------------
    def probe(name: str, source: str, expect_hit: bool) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="sec-self-check-"))
        (tmp / "server.js").write_text(source, encoding="utf-8")
        hits = _residue_hits(tmp, help_rules)
        check(f"residue.{name}", (hits > 0) if expect_hit else (hits == 0),
              f"hits={hits} expect_hit={expect_hit}")

    def probe_files(name: str, files: dict, expect_hit: bool) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="sec-self-check-"))
        for relative, source in files.items():
            target = tmp / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
        hits = _residue_hits(tmp, help_rules)
        check(f"residue.{name}", (hits > 0) if expect_hit else (hits == 0),
              f"hits={hits} expect_hit={expect_hit}")

    probe("plain-source-detected", "const express = require('express')();\nres.sendStatus(200);\n", True)
    probe("concat-source-detected", "const express = require('expr' + 'ess');\n", True)
    probe("wrapped-source-detected", "const express = require(String('express'));\n", True)
    probe("join-source-detected", "const express = require(['ex', 'press'].join(''));\n", True)
    probe("charcode-source-detected", "const express = require(String.fromCharCode(101,120,112,114,101,115,115));\n", True)
    probe("variable-indirection-detected", "const LEGACY = 'express';\nconst express = require(LEGACY);\n", True)
    probe("variable-split-concat-detected",
          "const __head = 'expr';\nconst __tail = 'ess';\nconst express = require(__head + __tail);\n", True)
    probe("require-alias-detected",
          "const __resolveModule = require;\nconst express = __resolveModule('express');\n", True)
    probe("buffer-hex-detected", "const express = require(Buffer.from('65787072657373', 'hex').toString());\n", True)
    probe("buffer-base64-detected", "const express = require(Buffer.from('ZXhwcmVzcw==', 'base64').toString());\n", True)
    probe("unicode-escape-detected", "const express = require('expre\\u0073s');\n", True)
    probe_files("sidefile-detected",
                {"server.js": "require('./server/legacyExpressFallback');\n",
                 "server/legacyExpressFallback.js": "module.exports = require('express');\n"},
                True)
    probe_files("opaque-base64-payload-detected",
                {"server/runtime/payload.bin":
                 base64.b64encode(b'const legacy = require("express");\nlegacy().listen(0);\n').decode()},
                True)
    probe_files("opaque-inline-payload-detected",
                {"server.js":
                 "const __legacyBlob = '"
                 + base64.b64encode(b'const legacy = require("express");\nlegacy().listen(0);\n').decode()
                 + "';\nconst helper = require('path').join(__dirname, 'h.js');\n"
                   "require('fs').writeFileSync(helper, Buffer.from(__legacyBlob, 'base64'));\n"
                   "require('child_' + 'process')['spawn'](process.execPath, [helper], { detached: true })['unref']();\n"},
                True)
    probe("join-separator-legal-inert", "const proc = require(['child', 'process'].join('_'));\n", False)
    probe_files("opaque-inline-inert",
                {"server.js":
                 "const fastify = require('fastify')();\n"
                 "const digest = 'QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=';\n"
                 "fastify.get('/', (req, reply) => reply.send(digest));\n"},
                False)
    probe("comment-inert", "// Mimics Express res.sendStatus()\nconst app = 1;\n", False)
    probe("legal-equivalent-inert",
          "const fastify = require('fastify')();\n"
          "fastify.register(require('@fastify/static'));\n"
          "const opts = { bodyLimit: 100 * 1024, host: \"0.0.0.0\" };\n"
          "reply.send(String('OK'));\n", False)

    # --- runtime module-identity probe classification --------------------
    from evidence import _runtime_source_fallback  # noqa: E402

    def runtime_check(name: str, records: list, expect_hit: bool) -> None:
        hits = _runtime_source_fallback(
            [{"kind": "target-runtime-probe", "records": records}], policy
        )
        check(f"runtime.{name}", bool(hits) if expect_hit else (not hits),
              f"hits={len(hits)} expect_hit={expect_hit}")

    runtime_check(
        "require-detected",
        [{"type": "require", "request": "express", "parent": "/app/server.js"}],
        True,
    )
    runtime_check(
        "child-payload-detected",
        [{"type": "child", "method": "spawn", "command": "node",
          "args": ["-e", 'require("express").listen(0)']}],
        True,
    )
    runtime_check(
        "helper-require-detected",
        [{"type": "require", "request": "child_process", "parent": "/app/server.js"},
         {"type": "child", "method": "spawn", "command": "/usr/local/bin/node",
          "args": ["/tmp/odc-helper-451.js"]},
         {"type": "require", "request": "express", "parent": "/tmp/odc-helper-451.js"}],
        True,
    )
    runtime_check(
        "legal-inert",
        [{"type": "require", "request": "fastify", "parent": "/app/server.js"},
         {"type": "require", "request": "@fastify/static", "parent": "/app/server.js"},
         {"type": "child", "method": "spawn", "command": "darknet", "args": ["yolov3"]}],
        False,
    )

    failed = [c for c in checks if not c["ok"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if not failed else "FAIL",
        "checks": checks,
        "failed": [c["name"] for c in failed],
    }


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Migration verifier security self-check")
    parser.add_argument("--root", type=Path, default=None, help="tests root (default: parent of this file)")
    parser.add_argument("--json", action="store_true", help="emit the full result as JSON")
    args = parser.parse_args(argv)
    result = run_security_self_check(args.root)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for row in result["checks"]:
            print(f"[{'ok' if row['ok'] else 'FAIL'}] {row['name']}: {row['detail']}")
        print(f"security self-check {result['status']} ({len(result['checks'])} checks)")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Calibration self-check for the BATCH-F2P-0003 residue/fallback rules.

For every rule in the authored fragment this verifier asserts:

1. the ``target`` is absent from the reference target files matched by ``paths``
   (the migration removed it);
2. applying the rule's ``calibration`` to a scratch copy of the reference target
   makes the *same* rule scanner hit the calibrated path;
3. the declared ``source_evidence`` really contains the ``target`` in the source
   repository.

Run with the authoring mounts available (``/input/target`` and ``/input/source``).
"""

import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
FRAGMENT = os.path.join(ASSET_ROOT, "fragments", "BATCH-F2P-0003.json")
TARGET_ROOT = os.environ.get("TARGET_ROOT", "/input/target")
SOURCE_ROOT = os.environ.get("SOURCE_ROOT", "/input/source")


def _read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _scan(text, rule):
    kind = rule["kind"]
    target = rule["target"]
    if kind == "literal":
        return target in text
    if kind == "import":
        pattern = re.compile(r"(^|\s)(?:import|from)\s+" + re.escape(target) + r"\b", re.MULTILINE)
        return bool(pattern.search(text))
    if kind == "dependency":
        pattern = re.compile(r"(?m)^\s*" + re.escape(target) + r"\s*($|[=<>~! ])")
        return bool(pattern.search(text))
    if kind == "file":
        return True
    raise ValueError("unknown rule kind: %s" % kind)


def _all_rules(fragment):
    rules = []
    for key in ("semantic_rules", "ordinary_residue_rules", "deceptive_fallback_rules"):
        for rule in fragment.get(key, []) or []:
            rule = dict(rule)
            rule["_bucket"] = key
            rules.append(rule)
    return rules


def main():
    fragment = json.loads(_read(FRAGMENT))
    rules = _all_rules(fragment)
    failures = []
    for rule in rules:
        rule_id = rule["id"]
        for rel in rule["paths"]:
            target_path = os.path.join(TARGET_ROOT, rel)
            if not os.path.exists(target_path):
                failures.append("%s: target path missing %s" % (rule_id, rel))
                continue
            if _scan(_read(target_path), rule):
                failures.append("%s: target %r still present in reference target %s" % (rule_id, rule["target"], rel))
        scratch = tempfile.mkdtemp(prefix="calib-")
        work = os.path.join(scratch, "work")
        shutil.copytree(TARGET_ROOT, work, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        calibration = rule["calibration"]
        cal_path = os.path.join(work, calibration["path"])
        with open(cal_path, "a", encoding="utf-8") as handle:
            if calibration.get("operation", "append") == "append":
                handle.write("\n" + calibration["content"] + "\n")
            else:
                handle.write(calibration["content"])
        if not _scan(_read(cal_path), rule):
            failures.append("%s: calibrated path %s did not trigger the rule" % (rule_id, calibration["path"]))
        shutil.rmtree(scratch, ignore_errors=True)
        for evidence in rule["source_evidence"]:
            source_path = os.path.join(SOURCE_ROOT, evidence["path"])
            if not os.path.exists(source_path):
                failures.append("%s: source evidence path missing %s" % (rule_id, evidence["path"]))
                continue
            lines = _read(source_path).splitlines()
            start = max(1, int(evidence["start_line"]))
            end = min(len(lines), int(evidence["end_line"]))
            segment = "\n".join(lines[start - 1:end])
            if not _scan(segment, rule):
                failures.append(
                    "%s: source evidence %s:%s-%s lacks %r" % (rule_id, evidence["path"], start, end, rule["target"])
                )
    if failures:
        print("CALIBRATION FAILURES:")
        for item in failures:
            print("  -", item)
        return 1
    print("calibrated %d rules successfully" % len(rules))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

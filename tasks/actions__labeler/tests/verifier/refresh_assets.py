"""Regenerate public projections and hashes without Go or Docker.

Run after editing evaluator assets or solution.patch. --check verifies that all
generated files are current without writing them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from evidence import build_source_test_baseline

ROOT = Path(__file__).resolve().parent.parent


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def command(*args):
    return subprocess.run(args, check=True, capture_output=True).stdout


def generated():
    process_policy = ROOT / 'verifier/detectors/process_commands.py'
    policy_projection = 'assets/target/blackbox/process_commands.py'
    updates = {policy_projection: process_policy.read_bytes()}
    with tempfile.TemporaryDirectory(prefix="labeler-projections-") as temporary:
        work = Path(temporary)
        projection = work / "projection"
        projection.mkdir()
        command("git", "init", "--quiet", str(projection))
        command("git", "-C", str(projection), "apply", str(ROOT / "verifier/test.patch"))
        for tree in ("shared", "target", "fixtures"):
            shutil.copytree(ROOT / "assets" / tree, projection / "tests" / tree,
                            dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (projection / 'tests/target/blackbox/process_commands.py').write_bytes(updates[policy_projection])
        # The original source projection is kept in test.patch. Both sides use
        # the same harness code and shared corpus; only their runtime differs.
        shutil.copy2(ROOT / "assets/target/runner", projection / "tests/source/runner")
        command("git", "-C", str(projection), "add", "-N", "tests")
        updates["verifier/test.patch"] = command("git", "-C", str(projection), "diff", "--binary", "--", "tests")

        reference = work / "reference"
        command("git", "clone", "--quiet", str(ROOT / "assets/build-deps/repo/base-repo.bundle"), str(reference))
        command("git", "-C", str(reference), "apply", "--binary", str(ROOT.parent / "solution/solution.patch"))
        updates["assets/target-test-baseline.json"] = encoded(build_source_test_baseline(reference))

    config = json.loads((ROOT / "config.json").read_text())
    paths = sorted({path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*")
                    if path.is_file() and "__pycache__" not in path.relative_to(ROOT).parts} | set(updates))
    config["public_contract"]["allowed_files"] = paths
    updates["config.json"] = encoded(config)
    manifest = json.loads((ROOT / "assets/assets-manifest.json").read_text())
    manifest["files"] = []
    for relative in paths:
        if relative == "assets/assets-manifest.json":
            continue
        data = updates.get(relative)
        if data is None:
            data = (ROOT / relative).read_bytes()
        manifest["files"].append({"path": relative, "size": len(data),
                                  "sha256": "sha256:" + hashlib.sha256(data).hexdigest()})
    updates["assets/assets-manifest.json"] = encoded(manifest)
    return updates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    updates = generated()
    changed = [relative for relative, data in updates.items()
               if not (ROOT / relative).exists() or (ROOT / relative).read_bytes() != data]
    if args.check:
        if changed:
            print("stale generated assets: " + ", ".join(changed))
            return 1
        print("public projections, reference test baseline and integrity hashes are current")
        return 0
    for relative in changed:
        path = ROOT / relative
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(updates[relative])
        temporary.replace(path)
    print("updated: " + (", ".join(changed) if changed else "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

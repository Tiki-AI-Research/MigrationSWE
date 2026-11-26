#!/usr/bin/env python3
"""Enable order evidence only after recollecting it from the frozen source.

Nothing in tasks/tests is written until the pristine source has been rebuilt
with Node 24 and Zod 4.0.5, all legacy observations match, and staged checks
pass. Candidate code and candidate observations are never baseline inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True


TESTS = Path(__file__).resolve().parents[1]
TASKS = TESTS.parent
ORDER_PATCH = Path(__file__).with_name("order-coverage.patch")


def run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=600,
    )
    if result.returncode:
        raise RuntimeError(f"{argv[0]} exited {result.returncode}:\n{result.stderr[-5000:]}\n{result.stdout[-2000:]}")
    return result.stdout


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def frozen_source_revision(bundle: Path, env: dict[str, str]) -> str:
    # The public bundle is integrity-checked before this call. Its exported
    # HEAD is the frozen repository identity; an upstream task commit label
    # may differ after the repository has been packaged as a single commit.
    heads = run(["git", "bundle", "list-heads", str(bundle), "HEAD"],
                cwd=bundle.parent, env=env).splitlines()
    if len(heads) != 1 or not re.fullmatch(r"[0-9a-f]{40} HEAD", heads[0]):
        raise RuntimeError("frozen source bundle must export exactly one HEAD")
    return heads[0].split()[0]


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sync_embedded_assets(tests: Path) -> None:
    replacements = {
        "shared/batches/BATCH-DIFFERENTIAL-0001/cases.mjs":
            (tests / "assets/shared/batches/BATCH-DIFFERENTIAL-0001/cases.mjs").read_text(),
        "fixtures/batches/BATCH-DIFFERENTIAL-0001/expected.json":
            (tests / "assets/fixtures/batches/BATCH-DIFFERENTIAL-0001/expected.json").read_text(),
    }
    target = tests / "assets/target/batches/BATCH-DIFFERENTIAL-0002"
    for side in ("source", "target"):
        replacements[f"{side}/batches/BATCH-DIFFERENTIAL-0002/probe.mjs"] = (target / "probe.mjs").read_text()
        expected = json.loads((target / "expected.json").read_text())
        expected["side"] = side
        replacements[f"{side}/batches/BATCH-DIFFERENTIAL-0002/expected.json"] = json.dumps(expected, indent=2) + "\n"
    patch = tests / "verifier/test.patch"
    blocks = re.split(r"(?=^diff --git )", patch.read_text(), flags=re.M)
    updated: set[str] = set()
    for index, block in enumerate(blocks):
        match = re.search(r"^\+\+\+ b/tests/(.+)$", block, re.M)
        if not match or match[1] not in replacements:
            continue
        relative = match[1]
        if "new file mode" not in block:
            raise RuntimeError(f"expected an additive embedded asset: {relative}")
        lines = replacements[relative].splitlines(keepends=True)
        header = block[:block.index("@@")]
        blocks[index] = header + f"@@ -0,0 +1,{len(lines)} @@\n" + "".join("+" + line for line in lines)
        updated.add(relative)
    if updated != set(replacements):
        raise RuntimeError(f"embedded asset inventory differs: {set(replacements) - updated}")
    patch.write_text("".join(blocks))


def refresh_manifest(tests: Path) -> None:
    manifest_path = tests / "assets/assets-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for row in manifest["files"]:
        data = (tests / row["path"]).read_bytes()
        row.update(sha256=digest(data), size=len(data))
    write_json(manifest_path, manifest)


def collect(tests: Path, source: Path, node: str, env: dict[str, str]) -> tuple[dict, dict]:
    assets = tests / "assets"
    output = tests.parent / "source-order-0001.json"
    source_env = {**env, "MIGRATION_REPO_ROOT": str(source), "REPO_ROOT": str(source)}
    # The shared/target files are the same observer implementation for either
    # role. Only the pristine source checkout is loaded here.
    run([node, str(assets / "target/batches/BATCH-DIFFERENTIAL-0001/probe.mjs"), str(output)],
        cwd=source, env=source_env)
    first = json.loads(output.read_text())
    second = json.loads(run(
        [node, str(assets / "target/batches/BATCH-DIFFERENTIAL-0002/probe.mjs"), str(source), str(assets)],
        cwd=source, env=source_env,
    ))
    if first.get("passed") is not True or second.get("ok") is not True:
        raise RuntimeError("source probes did not pass; no assets were changed")
    return first, second


def update_snapshots(tests: Path, first: dict, second: dict, provenance: dict) -> None:
    path = tests / "assets/source-baseline-outputs.json"
    baseline = json.loads(path.read_text())
    by_id = {row["id"]: row for row in baseline["observations"]}
    values = {key: row["normalized"] for key, row in first["results"].items()}
    values.update(second["observations"])
    if set(values) != {f"DIFF-{number:03}" for number in range(1, 37)}:
        raise RuntimeError("source differential observation inventory differs")
    for identifier, value in values.items():
        if identifier in first["results"]:
            legacy = value["value"]
        else:
            legacy = json.loads(json.dumps(value))
            for check in legacy["checks"]:
                check.pop("serializedOutputs", None)
        if canonical(legacy) != canonical(by_id[identifier]["value"]["value"]):
            raise RuntimeError(f"legacy source behavior differs for {identifier}; baseline was not overwritten")
        by_id[identifier]["value"]["value"] = value
    baseline["order_baseline_provenance"] = provenance
    write_json(path, baseline)

    fixture_path = tests / "assets/fixtures/batches/BATCH-DIFFERENTIAL-0001/expected.json"
    fixture = json.loads(fixture_path.read_text())
    fixture["expected"] = {key: row["normalized"] for key, row in first["results"].items()}
    write_json(fixture_path, fixture)

    expected_path = tests / "assets/target/batches/BATCH-DIFFERENTIAL-0002/expected.json"
    expected = json.loads(expected_path.read_text())
    for identifier, observation in second["observations"].items():
        expected["observations"][identifier]["ok"] = observation["ok"]
        expected["observations"][identifier]["normalized_sha256"] = digest(canonical(observation).encode())
        expected["observations"][identifier]["check_names"] = [check["name"] for check in observation["checks"]]
    expected["normalized_sha256"] = digest(canonical(second["observations"]).encode())
    write_json(expected_path, expected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependencies", type=Path, required=True, help="trusted source node_modules directory, including build tools and Zod 4.0.5")
    parser.add_argument("--node", default="node", help="Node 24 executable")
    parser.add_argument("--npm", default="npm", help="npm executable for the source build")
    args = parser.parse_args()
    # Direct script execution otherwise finds verifier/verifier.py before
    # the verifier namespace package, even after adding TESTS to sys.path.
    verifier_dir = Path(__file__).resolve().parent
    sys.path = [entry for entry in sys.path
                if entry and Path(entry).resolve() != verifier_dir]
    sys.path.insert(0, str(TESTS))
    from verifier.integrity import validate_public_integrity
    validate_public_integrity(TESTS)
    dependencies = args.dependencies.resolve()
    node = shutil.which(args.node)
    npm = shutil.which(args.npm)
    if not node or not npm:
        raise RuntimeError("Node 24 and npm must be available")
    env = {**os.environ, "PATH": str(Path(node).parent) + os.pathsep + os.environ.get("PATH", ""),
           "PYTHONDONTWRITEBYTECODE": "1", "CI": "1"}
    for key in ("NODE_OPTIONS", "NODE_PATH", "MIGRATION_REPO_ROOT", "REPO_ROOT", "MIGRATION_BASELINE_ROOT"):
        env.pop(key, None)
    node_version = run([node, "--version"], cwd=TASKS, env=env).strip()
    if not node_version.startswith("v24."):
        raise RuntimeError(f"source recollection requires Node 24, got {node_version}")
    zod_package = dependencies / "zod/package.json"
    if not zod_package.is_file() or json.loads(zod_package.read_text()).get("version") != "4.0.5":
        raise RuntimeError("source recollection requires actual Zod 4.0.5; no assets were changed")
    original = {p.relative_to(TESTS): p.read_bytes() for p in TESTS.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts}

    with tempfile.TemporaryDirectory(prefix="zod-source-order-") as temporary:
        root = Path(temporary)
        tests = root / "tasks/tests"
        shutil.copytree(TESTS, tests, ignore=shutil.ignore_patterns("__pycache__"))
        run(["git", "apply", "--check", str(ORDER_PATCH)], cwd=root, env=env)
        run(["git", "apply", str(ORDER_PATCH)], cwd=root, env=env)
        source = root / "source"
        bundle = TESTS / "assets/build-deps/repo/base-repo.bundle"
        run(["git", "clone", "--quiet", str(bundle), str(source)], cwd=root, env=env)
        revision = run(["git", "rev-parse", "HEAD"], cwd=source, env=env).strip()
        expected_revision = frozen_source_revision(bundle, env)
        if revision != expected_revision:
            raise RuntimeError(f"source revision differs: expected {expected_revision}, got {revision}")
        (source / "node_modules").symlink_to(dependencies, target_is_directory=True)
        actual_version = run([node, "-p", "require('zod/package.json').version"], cwd=source, env=env).strip()
        if actual_version != "4.0.5":
            raise RuntimeError(f"source loaded unexpected Zod {actual_version}")
        print("Rebuilding the pristine source and collecting order evidence...", flush=True)
        run([npm, "run", "build"], cwd=source, env=env)
        first, second = collect(tests, source, node, env)
        update_snapshots(tests, first, second, {
            "zod_version": actual_version, "node_version": node_version,
            "source_revision": revision, "source_bundle_sha256": digest(bundle.read_bytes()),
            "first_probe_sha256": digest((tests / "assets/shared/batches/BATCH-DIFFERENTIAL-0001/cases.mjs").read_bytes()),
            "second_probe_sha256": digest((tests / "assets/target/batches/BATCH-DIFFERENTIAL-0002/probe.mjs").read_bytes()),
        })
        sync_embedded_assets(tests)
        refresh_manifest(tests)
        run([sys.executable, str(tests / "verifier/selfcheck.py")], cwd=root, env=env)
        staged = {p.relative_to(tests): p.read_bytes() for p in tests.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts}
        if staged.keys() != original.keys():
            raise RuntimeError("staged public asset inventory differs")
        changed = [relative for relative in original if original[relative] != staged[relative]]
        current = {p.relative_to(TESTS): p.read_bytes() for p in TESTS.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts}
        if current != original:
            raise RuntimeError("task assets changed during recollection; refusing to overwrite")
        written: list[Path] = []
        try:
            for relative in changed:
                written.append(relative)
                (TESTS / relative).write_bytes(staged[relative])
        except BaseException:
            for relative in written:
                (TESTS / relative).write_bytes(original[relative])
            raise
        print("Enabled order coverage after trusted source recollection:")
        for relative in changed:
            print(f"  tasks/tests/{relative}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Order baseline refresh failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

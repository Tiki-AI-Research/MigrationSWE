#!/usr/bin/env python3
"""Target-side differential probe for BATCH-DIFFERENTIAL-0001.

The INTEGRATION batch assembles the final ``harness.py``; this file deliberately
does not live at that path. It runs the private Go corpus probe against the
candidate Go module, compares the observed values with the shared corpus using
the batch's ``equal_after_normalization`` relation, and emits a report envelope
compatible with the SKELETON report helpers.

Usage: entry.py <repo-dir> [report-path]
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

BATCH_ID = "BATCH-DIFFERENTIAL-0001"
ASSETS_DIR = os.environ.get("MIGRATION_ASSETS_DIR", "/assets")
DEPENDENCY_MANIFEST = os.path.join(ASSETS_DIR, "dependencies", "dependency-manifest.json")

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE_GO = os.path.join(HERE, "probe", "main.go")


def _find_upwards(relative, start=HERE):
    """Locate ``relative`` by walking up from ``start``."""
    current = start
    while True:
        candidate = os.path.join(current, relative)
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _load_differential():
    """Import the shared differential helper module."""
    path = _find_upwards(os.path.join("shared", "batches", BATCH_ID, "differential.py"))
    if path is None:
        raise SystemExit("differential.py not found; is the shared asset assembled?")
    spec = importlib.util.spec_from_file_location("bdi_differential", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_report_helpers():
    """Import the SKELETON report helpers when available."""
    path = _find_upwards(os.path.join("shared", "skeleton", "report.py"))
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location("bdi_skeleton_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_corpus():
    path = _find_upwards(os.path.join("fixtures", "batches", BATCH_ID, "corpus.json"))
    if path is None:
        raise SystemExit("corpus.json not found; is the fixture asset assembled?")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _consume_dependency_manifest():
    with open(DEPENDENCY_MANIFEST, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    batch_ids = {entry.get("batch_id") for entry in manifest.get("batches", [])}
    if "BATCH-AUTHOR-BATCH-SKELETON-0001" not in batch_ids:
        raise SystemExit("dependency snapshot is missing the SKELETON batch")
    return manifest


def _run_probe(repo):
    """Run the Go probe against ``repo`` in an isolated temporary module."""
    workdir = tempfile.mkdtemp(prefix="bdi-probe-")
    try:
        shutil.copyfile(PROBE_GO, os.path.join(workdir, "main.go"))
        with open(os.path.join(workdir, "go.mod"), "w", encoding="utf-8") as handle:
            handle.write("module diffprobe\n\ngo 1.23.0\n\n")
            handle.write("require itertools v0.0.0\n\n")
            handle.write("replace itertools => %s\n" % os.path.abspath(repo))
        completed = subprocess.run(
            ["go", "run", "."],
            cwd=workdir,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise SystemExit("go probe failed:\n%s" % completed.stderr)
        return json.loads(completed.stdout)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv):
    repo = argv[1] if len(argv) > 1 else "."
    report_path = argv[2] if len(argv) > 2 else None

    _consume_dependency_manifest()
    differential = _load_differential()
    report_helpers = _load_report_helpers()
    corpus = _load_corpus()
    target_results = _run_probe(repo)

    results = []
    for case in corpus["cases"]:
        case_id = case["id"]
        passed = differential.equal_after_normalization(case["expected"], target_results.get(case_id))
        results.append({
            "observation_id": case["observation_id"],
            "passed": bool(passed),
            "detail": "%s: expected=%s target=%s" % (
                case_id,
                json.dumps(differential.normalize(case["expected"]), sort_keys=True),
                json.dumps(differential.normalize(target_results.get(case_id)), sort_keys=True),
            ),
        })

    if report_helpers is not None:
        entries = [
            report_helpers.make_result(r["observation_id"], r["passed"], r["detail"])
            for r in results
        ]
        report = report_helpers.make_report(BATCH_ID, entries)
    else:
        report = {"schema_version": "migration-result:v1", "batch_id": BATCH_ID, "results": results}

    text = json.dumps(report, indent=2, sort_keys=True)
    if report_path:
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")

    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

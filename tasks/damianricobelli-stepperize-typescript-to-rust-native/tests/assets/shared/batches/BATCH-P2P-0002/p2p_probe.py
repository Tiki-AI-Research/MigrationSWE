#!/usr/bin/env python3
"""Deterministic P2P projection probe.

Runs language-agnostic, dependency-free checks for the observations whose
surfaces live outside the migrated package (the docs registry, the react
package exports, and the protected public artifacts). Those artifacts are
byte-identical on the TypeScript source repository and the Rust target
repository, so the emitted projection is identical on both sides after
normalization. No network, no node, no cargo are required.
"""
import argparse
import json
import os
import re
import sys

REGISTRY_DEPENDENCY = "@stepperize/react"
REACT_PACKAGE = "packages/react/package.json"
REFERENCE_TEST = "packages/core/src/tests/utils.test.ts"
CORE_UTILS = "packages/core/src/utils.ts"


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def load_block_ids(repo):
    catalog = os.path.join(repo, "apps/docs/src/lib/blocks/catalog.mjs")
    text = read_text(catalog)
    start = text.index("export const BLOCKS = [")
    end = text.index("\n];", start)
    body = text[start:end]
    return re.findall(r'id:\s*"([^"]+)"', body)


def docs_registry_projection(repo):
    registry_dir = os.path.join(repo, "apps/docs/public/r")
    block_ids = load_block_ids(repo)
    items = []
    for name in sorted(os.listdir(registry_dir)):
        if not name.endswith(".json") or name == "registry.json":
            continue
        item = json.loads(read_text(os.path.join(registry_dir, name)))
        files = item.get("files") or []
        content = files[0].get("content", "") if files else ""
        deps = item.get("dependencies") or []
        items.append(
            {
                "id": name[: -len(".json")],
                "name_matches": item.get("name") == name[: -len(".json")],
                "has_inline_content": len(content) > 0,
                "has_stepperize_dependency": REGISTRY_DEPENDENCY in deps,
            }
        )
    present = {entry["id"] for entry in items}
    return {
        "operation": "docs.registry.blocks",
        "block_count": len(block_ids),
        "registry_item_count": len(items),
        "all_items_present": set(block_ids).issubset(present),
        "all_have_inline_content": all(e["has_inline_content"] for e in items),
        "all_have_stepperize_dependency": all(
            e["has_stepperize_dependency"] for e in items
        ),
        "orphan_registry_items": sorted(present - set(block_ids)),
    }


def package_exports(repo):
    package = json.loads(read_text(os.path.join(repo, REACT_PACKAGE)))
    return package.get("exports") or {}


def export_projection(repo, export):
    return {
        "package": "@stepperize/react",
        "export": export,
        "present": export in package_exports(repo),
    }


def react_primitives_projection(repo):
    return export_projection(repo, "./primitives")


def react_root_projection(repo):
    return export_projection(repo, ".")


def protected_artifacts_projection(repo):
    reference = os.path.join(repo, REFERENCE_TEST)
    utils = os.path.join(repo, CORE_UTILS)
    reference_text = read_text(reference) if os.path.exists(reference) else ""
    utils_text = read_text(utils) if os.path.exists(utils) else ""
    return {
        "reference_test_present": os.path.exists(reference),
        "protected_error_wording_present": (
            'not found.' in utils_text
            and "No match handler found for step" in utils_text
        ),
        "standard_schema_boundary_present": '"~standard"' in utils_text,
        "named_exports_present": all(
            name in reference_text
            for name in (
                "createStepMap",
                "getInitialStepIndex",
                "parseStep",
                "getInitialData",
                "validateStep",
                "getStepStatus",
                "getStepStatuses",
                "matchStep",
            )
        ),
    }


PROBES = {
    "P2P-026": react_primitives_projection,
    "P2P-029": docs_registry_projection,
    "P2P-036": react_root_projection,
    "P2P-037": protected_artifacts_projection,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.getcwd())
    parser.add_argument("--observation", action="append", default=[])
    parser.add_argument("--out")
    args = parser.parse_args()

    wanted = args.observation or list(PROBES)
    projection = {}
    results = {}
    for observation in wanted:
        probe = PROBES.get(observation)
        if probe is None:
            continue
        try:
            projection[observation] = probe(args.repo)
            results[observation] = "PASS"
        except Exception as exc:  # noqa: BLE001
            projection[observation] = {"error": str(exc)}
            results[observation] = "FAIL"

    payload = {"side_projection": projection, "side_results": results}
    text = json.dumps(payload, sort_keys=True, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 0 if all(value == "PASS" for value in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

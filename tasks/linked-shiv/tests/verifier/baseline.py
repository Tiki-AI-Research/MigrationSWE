"""Check frozen P2P case counts without importing candidate code or pytest."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Mapping


_TEST_FILES = ("test_migration_p2p.py", "test_dependency.py")
_TEST_ID = re.compile(r"test_p2p_(\d{3})(?:_|$)")


def _decorator_name(node: ast.expr) -> str:
    value = node.func if isinstance(node, ast.Call) else node
    return ast.unparse(value)


def _parameter_count(node: ast.expr) -> int:
    # Values need not be literals: (shiv_main, '-h') is a statically sized row.
    # Dynamic parameter generators need explicit collection support rather than
    # a guessed count that could silently certify an obsolete source snapshot.
    if (not isinstance(node, (ast.List, ast.Tuple)) or not node.elts
            or any(isinstance(item, ast.Starred) for item in node.elts)):
        raise ValueError("parameter values must be a nonempty static list or tuple")
    return len(node.elts)


def collected_p2p_counts(assets: Path) -> dict[str, int]:
    """Count this suite's cases, including shared parametrized fixture dependencies."""
    module_fixtures: dict[str, dict[str, tuple[ast.FunctionDef, int]]] = {}
    tests: list[tuple[str, ast.FunctionDef]] = []
    for name in ("conftest.py", *_TEST_FILES):
        fixtures: dict[str, tuple[ast.FunctionDef, int]] = {}
        tree = ast.parse((assets / "candidate-test" / name).read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if name in _TEST_FILES and _TEST_ID.match(node.name):
                tests.append((name, node))
            for decorator in node.decorator_list:
                if _decorator_name(decorator) != "pytest.fixture":
                    continue
                count = 1
                if isinstance(decorator, ast.Call):
                    for keyword in decorator.keywords:
                        if keyword.arg == "params":
                            count = _parameter_count(keyword.value)
                        elif keyword.arg in {"autouse", "name"}:
                            raise ValueError("fixture aliases and autouse fixtures require collection support")
                fixtures[node.name] = node, count
        module_fixtures[name] = fixtures

    counts: dict[str, int] = {}
    for module_name, test in tests:
        # Module-local fixtures override conftest only within their own module.
        # A fixture in test_dependency.py cannot affect migration test counts.
        fixtures = {**module_fixtures["conftest.py"], **module_fixtures[module_name]}
        identifier = "P2P-" + _TEST_ID.match(test.name).group(1)
        count = 1
        direct_parameters: set[str] = set()
        for decorator in test.decorator_list:
            if _decorator_name(decorator) != "pytest.mark.parametrize":
                continue
            if not isinstance(decorator, ast.Call) or len(decorator.args) < 2:
                raise ValueError(f"{identifier}: unsupported parametrization")
            for keyword in decorator.keywords:
                if keyword.arg == "indirect" and ast.literal_eval(keyword.value) is not False:
                    raise ValueError(f"{identifier}: indirect parameters require collection support")
            names = ast.literal_eval(decorator.args[0])
            if isinstance(names, str):
                names = [name.strip() for name in names.split(",")]
            if not isinstance(names, (list, tuple)) or not all(isinstance(name, str) and name for name in names):
                raise ValueError(f"{identifier}: invalid parameter names")
            direct_parameters.update(names)
            count *= _parameter_count(decorator.args[1])

        seen = set(direct_parameters)

        def fixture_count(name: str) -> int:
            if name in seen or name not in fixtures:
                return 1
            seen.add(name)
            fixture, total = fixtures[name]
            for argument in fixture.args.args:
                total *= fixture_count(argument.arg)
            return total

        for argument in test.args.args:
            count *= fixture_count(argument.arg)
        counts[identifier] = counts.get(identifier, 0) + count
    return counts


def source_baseline_errors(
    assets: Path, config: Mapping[str, Any], baseline: Mapping[str, Any],
) -> tuple[str, ...]:
    """Reject an evaluator/baseline mismatch before it becomes a candidate failure."""
    try:
        counts = collected_p2p_counts(assets)
        expected = {row["id"] for row in config["tests"]["p2p"]}
        errors = []
        if set(counts) != expected:
            errors.append("collected P2P IDs do not match the configured observation inventory")
        rows = baseline.get("observations", [])
        if not isinstance(rows, list):
            return ("source baseline observations must be an array",)
        by_id = {}
        for row in rows:
            if row.get("kind") == "P2P":
                if row["id"] in by_id:
                    errors.append(f"duplicate source observation: {row['id']}")
                by_id[row["id"]] = row
        if set(by_id) != expected:
            errors.append("source baseline P2P IDs do not match the configured observation inventory")
        for identifier in sorted(expected & set(by_id) & set(counts)):
            row = by_id[identifier]
            value = row["value"]
            outcomes = value["value"]["outcomes"]
            if (value.get("type") != "json" or value["value"].get("test") != identifier
                    or row.get("status") != "PASS" or not isinstance(outcomes, list)
                    or any(outcome != "PASS" for outcome in outcomes)):
                errors.append(f"{identifier}: source P2P baseline must contain passing typed outcomes")
            cases = row.get("evidence", {}).get("cases")
            if (not isinstance(outcomes, list) or len(outcomes) != counts[identifier]
                    or type(cases) is not int or cases != counts[identifier]):
                errors.append(f"{identifier}: evaluator collects {counts[identifier]} cases; "
                              f"source baseline records {cases!r}; regenerate or align the source baseline")
        if baseline.get("base_commit") != config.get("base_commit"):
            errors.append("source baseline base commit does not match the task")
        return tuple(errors)
    except (OSError, SyntaxError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return (f"source baseline case validation could not complete: {exc}",)

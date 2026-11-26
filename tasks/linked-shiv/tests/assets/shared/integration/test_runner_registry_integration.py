"""Integration contract tests for the shiv click 6.7 -> click 8.1.7 migration.

These checks wire the source and target test trees together and verify that the
shared runner registry contract, the shared fixtures, and the end-to-end pyz
build/run behavior are preserved by the migration.  The suite is expected to
pass on both trees and does not assert anything click-version specific.
"""

import importlib
import json
import subprocess
import sys

from pathlib import Path

import pytest


SOURCE_REGISTRY = "source.runner_registry"
TARGET_REGISTRY = "target.runner_registry"


def _load_registry(module_name):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # pragma: no cover - depends on the checkout layout
        pytest.skip("cannot import {!r}: {}".format(module_name, exc))


class TestRunnerRegistryIntegration:
    # The source and target trees must register the same public commands.
    def test_source_and_target_registries_expose_same_commands(self):
        source = _load_registry(SOURCE_REGISTRY)
        target = _load_registry(TARGET_REGISTRY)

        assert set(source.RUNNERS) == set(target.RUNNERS)
        assert set(source.RUNNERS) == {"shiv", "shiv-info"}

    # Both registries must expose an ``invoke`` entry point with the same shape.
    def test_source_and_target_registries_expose_invoke(self):
        source = _load_registry(SOURCE_REGISTRY)
        target = _load_registry(TARGET_REGISTRY)

        assert callable(source.invoke)
        assert callable(target.invoke)

    # The shared integration package must be importable from both trees.
    def test_shared_imports_resolve(self):
        shared = importlib.import_module("shared.integration")
        assert shared is not None


class TestEndToEndIntegration:
    # Build a pyz from the integration fixture package and execute it.
    def test_built_zipapp_executes(self, runner, shiv_root, tmp_path, package_location):
        output_file = Path(tmp_path) / "integration.pyz"

        result = runner(
            [
                "-e",
                "hello:main",
                "-o",
                str(output_file),
                "--site-packages",
                str(package_location),
            ]
        )

        assert result.exit_code == 0, result.output
        assert output_file.exists()

        proc = subprocess.run(
            [sys.executable, str(output_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        assert proc.returncode == 0, proc.stderr.decode()
        assert proc.stdout.decode().strip() == "hello world"

    # shiv-info must accept the built pyz and emit a JSON document.
    def test_shiv_info_json_round_trip(self, runner, info_runner, shiv_root, tmp_path, package_location):
        output_file = Path(tmp_path) / "integration-info.pyz"

        build = runner(
            [
                "-e",
                "hello:main",
                "-o",
                str(output_file),
                "--site-packages",
                str(package_location),
            ]
        )
        assert build.exit_code == 0, build.output

        info = info_runner([str(output_file), "--json"])

        assert info.exit_code == 0, info.output
        assert "build_id" in json.loads(info.output)

"""Shared fixtures for the BATCH-INTEGRATION-0001 integration tests.

The integration suite deliberately imports ``shiv`` lazily through the target
runner registry so that collection never fails on a tree where the migrated
package is not importable.
"""

import importlib

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo_root():
    """Absolute path to the asset repository root."""

    return REPO_ROOT


@pytest.fixture
def package_location():
    """The installable ``hello`` fixture package used by integration tests."""

    return REPO_ROOT / "fixtures" / "integration" / "package"


@pytest.fixture
def shiv_root(monkeypatch, tmp_path):
    """Point shiv's extraction root at an isolated temporary directory."""

    monkeypatch.setenv("SHIV_ROOT", str(tmp_path))
    return tmp_path


def _target_registry():
    return importlib.import_module("target.runner_registry")


@pytest.fixture
def runner():
    """Invoke the ``shiv`` console command through the target registry."""

    registry = _target_registry()
    return lambda args, env=None: registry.invoke("shiv", args, env=env)


@pytest.fixture
def info_runner():
    """Invoke the ``shiv-info`` console command through the target registry."""

    registry = _target_registry()
    return lambda args: registry.invoke("shiv-info", args)

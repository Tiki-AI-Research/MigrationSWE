"""Shared fixtures for the BATCH-P2P-0001 public-API contract tests.

The fixtures deliberately avoid importing ``shiv`` at collection time so the
same module can be consumed by both the source (click 6.7) and the target
(click 8.1.7) test trees.
"""

import os

from contextlib import contextmanager
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_ROOT = REPO_ROOT / "fixtures" / "batches" / "BATCH-P2P-0001"


@pytest.fixture
def repo_root():
    """Absolute path to the asset repository root."""

    return REPO_ROOT


@pytest.fixture
def fixtures_root():
    """Absolute path to this batch's fixtures directory."""

    return FIXTURES_ROOT


@pytest.fixture
def package_location():
    """The installable ``hello`` fixture package used by the CLI tests."""

    return FIXTURES_ROOT / "package"


@pytest.fixture
def shiv_root(monkeypatch, tmp_path):
    """Point shiv's extraction root at an isolated temporary directory."""

    monkeypatch.setenv("SHIV_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def runner():
    """Invoke the ``shiv`` console command through click's test runner."""

    from click.testing import CliRunner

    from shiv.cli import main as shiv_main

    def invoke(args, env=None):
        return CliRunner().invoke(shiv_main, list(args), env=env)

    return invoke


@pytest.fixture
def info_runner():
    """Invoke the ``shiv-info`` console command through click's test runner."""

    from click.testing import CliRunner

    from shiv.info import main as info_main

    def invoke(args):
        return CliRunner().invoke(info_main, list(args))

    return invoke


@pytest.fixture
def env_var():
    """Context manager that temporarily sets an environment variable."""

    @contextmanager
    def _env_var(key, value):
        previous = os.environ.get(key)
        os.environ[key] = value
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

    return _env_var

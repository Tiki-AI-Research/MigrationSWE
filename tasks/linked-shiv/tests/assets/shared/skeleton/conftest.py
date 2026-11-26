"""Shared test scaffolding for the shiv click 6.7 -> 8.1.7 migration.

This skeleton module intentionally avoids importing ``shiv`` at collection
time so that the same fixtures can be consumed by both the ``source`` and
``target`` test suites while the migration is in progress.
"""

import os

from contextlib import contextmanager
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "fixtures" / "skeleton"


@pytest.fixture
def repo_root():
    """Absolute path to the asset repository root."""

    return REPO_ROOT


@pytest.fixture
def fixtures_root():
    """Absolute path to the skeleton fixtures directory."""

    return FIXTURES_ROOT


@pytest.fixture
def package_location():
    """The installable ``hello`` fixture package used by CLI tests."""

    return FIXTURES_ROOT / "package"


@pytest.fixture
def zip_location():
    """A pre-built zipapp fixture, when one is present."""

    return FIXTURES_ROOT / "test.zip"


@pytest.fixture
def shiv_root(monkeypatch, tmp_path):
    """Point shiv's extraction root at an isolated temporary directory."""

    monkeypatch.setenv("SHIV_ROOT", str(tmp_path))
    return tmp_path


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

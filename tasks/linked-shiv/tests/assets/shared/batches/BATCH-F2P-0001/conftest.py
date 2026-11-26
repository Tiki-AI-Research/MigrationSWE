"""Shared fixtures for the shiv click 6.7 -> 8.1.7 migration F2P batch."""

from pathlib import Path

import pytest


def _find_shiv_repo_root():
    for candidate in Path(__file__).resolve().parents:
        setup_cfg = candidate / "setup.cfg"
        try:
            text = setup_cfg.read_text(encoding="utf-8")
        except OSError:
            continue
        if "name = shiv" in text:
            return candidate
    return None


@pytest.fixture
def shiv_repo_root():
    """Absolute path to the shiv project root under test."""

    root = _find_shiv_repo_root()
    if root is None:
        pytest.skip("could not locate the shiv project root")
    return root

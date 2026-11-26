"""Fail-to-pass checks for observation F2P-001.

The shiv project must declare and install click 8.1.7 and must no longer
reference the legacy click 6.7 requirement.
"""

import configparser
import os

from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version


TARGET_CLICK_VERSION = Version("8.1.7")
LEGACY_CLICK_VERSION = "6.7"


def _candidate_repo_roots():
    env_root = os.environ.get("SHIV_REPO_ROOT")
    if env_root:
        yield Path(env_root)
    yield from Path(__file__).resolve().parents
    yield Path.cwd()
    yield Path("/workspace/repo")


def _find_project_root():
    seen = set()
    for candidate in _candidate_repo_roots():
        try:
            key = str(candidate.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        setup_cfg = candidate / "setup.cfg"
        try:
            text = setup_cfg.read_text(encoding="utf-8")
        except OSError:
            continue
        if "name = shiv" in text:
            return candidate
    return None


def _click_requirement_from_text(text):
    for line in text.splitlines():
        line = line.strip()
        if not line.lower().startswith("click"):
            continue
        try:
            parsed = Requirement(line)
        except ValueError:
            continue
        if parsed.name.lower() == "click":
            return line
    return None


def _click_requirement_from_setup_cfg(path):
    parser = configparser.ConfigParser()
    parser.read(str(path))
    raw = parser.get("options", "install_requires", fallback="")
    return _click_requirement_from_text(raw)


def _declared_click_requirement():
    try:
        requirements = metadata.requires("shiv") or []
    except metadata.PackageNotFoundError:
        requirements = []

    for entry in requirements:
        try:
            parsed = Requirement(entry)
        except ValueError:
            continue
        if parsed.name.lower() == "click":
            return entry

    root = _find_project_root()
    if root is not None:
        return _click_requirement_from_setup_cfg(root / "setup.cfg")

    return None


def _installed_click_version():
    try:
        return metadata.version("click")
    except metadata.PackageNotFoundError:
        return None


def test_click_dependency_upgraded_to_8_1_7():
    requirement = _declared_click_requirement()
    assert requirement is not None, "shiv must declare a click dependency"

    assert LEGACY_CLICK_VERSION not in requirement, (
        "shiv must no longer declare the legacy click 6.7 requirement: {!r}".format(requirement)
    )

    parsed = Requirement(requirement)
    lower_bounds = [
        Version(spec.version) for spec in parsed.specifier if spec.operator in (">=", "==", "~=")
    ]
    assert lower_bounds, "click requirement must pin a minimum version: {!r}".format(requirement)
    assert min(lower_bounds) >= TARGET_CLICK_VERSION, (
        "click requirement must require at least 8.1.7: {!r}".format(requirement)
    )

    installed = _installed_click_version()
    if installed is not None:
        assert Version(installed) == TARGET_CLICK_VERSION, (
            "installed click must be 8.1.7, found {!r}".format(installed)
        )

"""Regression checks for evidence collection (not additional migration nodes)."""

import json

import pytest
from packaging.requirements import Requirement

from migration_support import isolated_python_env, normalize_info_output
from test_dependency import formal_requirements


@pytest.mark.parametrize("filename,content", [
    ("setup.cfg", "[options]\ninstall_requires =\n  pip\n[options.extras_require]\ncli =\n  click>=6.7,!=7.0\n"),
    ("tox.ini", "[testenv:py38]\ndeps =\n  py38: click>=6.7,!=7.0\n"),
    ("setup.py", "from setuptools import setup\nbase = ['pip']\nextra = ['click>=6.7,!=7.0']\nsetup(install_requires=base + extra)\n"),
    ("setup.py", "from setuptools import setup\nkwargs = {'extras_require': {'cli': ['click>=6.7,!=7.0']}}\nsetup(**kwargs)\n"),
    ("pyproject.toml", '[project.optional-dependencies]\ncli = ["click>=6.7,!=7.0"]\n'),
    ("pyproject.toml", '[build-system]\nrequires = ["click>=6.7,!=7.0"]\n'),
    ("pyproject.toml", '[dependency-groups]\nbase = ["click>=6.7,!=7.0"]\ndev = [{include-group = "base"}]\n'),
    ("pyproject.toml", '[tool.poetry.group.cli.dependencies]\nclick = ">=6.7,!=7.0"\n'),
    ("requirements.txt", "-r nested.txt\n"),
    ("requirements.txt", "-c nested.txt\n"),
])
def test_dependency_formats_expose_legacy_click(tmp_path, filename, content):
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    (tmp_path / "nested.txt").write_text("click>=6.7,!=7.0 # old constraint\n", encoding="utf-8")
    click = [Requirement(line) for line in formal_requirements(path)
             if Requirement(line).name.lower() == "click"]
    assert click
    assert all(req.specifier.contains("6.7") for req in click)
    assert all(str(req.specifier) != "==8.1.7" for req in click)


def test_unresolved_setup_dependencies_fail(tmp_path):
    path = tmp_path / "setup.py"
    path.write_text("from setuptools import setup\nsetup(install_requires=get_dependencies())\n")
    with pytest.raises(ValueError, match="Unresolved"):
        formal_requirements(path)


def test_requirement_include_cycle_fails(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("-r requirements.txt\n")
    with pytest.raises(ValueError, match="Cyclic"):
        formal_requirements(path)


def test_host_pythonpath_is_removed_but_fixture_is_preserved(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "host/source/src")
    monkeypatch.setenv("PYTHONHOME", "host/python")
    assert "PYTHONPATH" not in isolated_python_env()
    assert "PYTHONHOME" not in isolated_python_env()
    assert "PYTHONPATH" not in isolated_python_env({"PYTHONPATH": "host/source/src", "SHIV_ROOT": "cache"})
    assert isolated_python_env({"PYTHONPATH": "fixture"})["PYTHONPATH"] == "fixture"


def test_full_output_normalization_preserves_error_and_json_differences():
    windows = 'Traceback\n  File "C:\\src\\shiv\\info.py", line 14, in main\n    read(data)\nValueError: C:\\input\\plain.zip\n'
    unix = 'Traceback\n  File "/target/shiv/info.py", line 16, in main\n    read(data)\nValueError: /input/plain.zip\n'
    left = normalize_info_output(windows, "C:\\input\\plain.zip", ["C:\\src"])
    right = normalize_info_output(unix, "/input/plain.zip", ["/target"])
    assert left == right
    changed = unix.replace("read(data)", "read(other)")
    assert left != normalize_info_output(changed, "/input/plain.zip", ["/target"])
    assert left != normalize_info_output(unix.replace("ValueError", "RuntimeError"), "/input/plain.zip", ["/target"])
    json_a = json.dumps({"error": "invalid", "detail": "first"})
    json_b = json.dumps({"error": "invalid", "detail": "second"})
    assert normalize_info_output(json_a, "unused", []) != normalize_info_output(json_b, "unused", [])

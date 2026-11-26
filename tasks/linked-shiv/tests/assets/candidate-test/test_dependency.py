"""Dependency migration tests: target declaration (F2P) and environment integrity (P2P)."""

import configparser
import ast
import re
import shutil
import subprocess
import sys
import tempfile
import tarfile
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path

import pytest

from migration_support import isolated_python_env, run_isolated
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet


def normalize_name(name: str) -> str:
    """Normalize package name per PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement(line: str):
    """Parse a single requirement specification without external packaging dependency."""
    cleaned = line.strip().split("#")[0].strip()
    if not cleaned:
        return None
    marker = None
    if ";" in cleaned:
        cleaned, marker = [part.strip() for part in cleaned.split(";", 1)]
    m = re.match(r"^([a-zA-Z0-9_\-\.]+)\s*(.*)$", cleaned)
    if not m:
        return None
    name = normalize_name(m.group(1))
    spec = m.group(2).strip().replace("(", "").replace(")", "").replace(" ", "")
    return {"name": name, "spec": spec, "marker": marker, "raw": line.strip()}


def formal_requirements(path: Path, seen=None):
    """Read dependency declarations structurally; fail on unresolved input.

    setup.py is inspected without executing arbitrary project code. Unsupported
    dynamic declarations produce an explicit error rather than false evidence.
    """
    path = path.resolve()
    seen = set() if seen is None else set(seen)
    if path in seen:
        raise ValueError(f"Cyclic dependency include: {path}")
    # ``seen`` is the current include stack, not a global visited set: the
    # same requirements file may be included by two independent branches.
    seen = {*seen, path}
    name = path.name.lower()

    def requirement_lines(text):
        values = []
        text = text.replace("\\\n", "")
        for raw in text.splitlines():
            line = re.split(r"\s+#", raw.strip(), maxsplit=1)[0].strip()
            if not line or line.startswith("#"):
                continue
            include = re.match(r"^(?:-r\s*|-c\s*|--requirement(?:=|\s+)|--constraint(?:=|\s+))(.+)$", line)
            if include:
                values.extend(formal_requirements(path.parent / include.group(1).strip(), seen))
            elif line.startswith(("--index-url", "--extra-index-url", "--find-links", "--no-index", "--trusted-host")):
                continue  # pip transport configuration, not a dependency
            else:
                line = re.sub(r"\s+--hash=\S+", "", line)
                values.append(line)
        return values

    if name in {"setup.cfg", "tox.ini"}:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path, encoding="utf-8")
        values = []
        if name == "setup.cfg":
            for key in ("install_requires", "setup_requires", "tests_require"):
                if parser.has_option("options", key):
                    values.extend(requirement_lines(parser.get("options", key)))
            if parser.has_section("options.extras_require"):
                for _, value in parser.items("options.extras_require"):
                    values.extend(requirement_lines(value))
        else:
            for section in parser.sections():
                if section == "testenv" or section.startswith("testenv:"):
                    if parser.has_option(section, "deps"):
                        for line in parser.get(section, "deps").splitlines():
                            # tox factor prefixes (e.g. py38: click==8.1.7).
                            line = re.sub(r"^[\w,!&-]+:\s+", "", line.strip())
                            values.extend(requirement_lines(line))
        return values

    if name == "pyproject.toml":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        project = data.get("project", {})
        values = list(data.get("build-system", {}).get("requires", []))
        values.extend(project.get("dependencies", []))
        for deps in project.get("optional-dependencies", {}).values():
            values.extend(deps)
        groups = data.get("dependency-groups", {})

        def group_deps(name, stack=()):
            if name in stack:
                raise ValueError(f"Cyclic dependency group: {name}")
            result = []
            for entry in groups[name]:
                if isinstance(entry, str):
                    result.append(entry)
                elif isinstance(entry, dict) and set(entry) == {"include-group"}:
                    result.extend(group_deps(entry["include-group"], stack + (name,)))
                else:
                    raise ValueError(f"Unsupported dependency group entry: {entry!r}")
            return result

        for group in groups:
            values.extend(group_deps(group))
        poetry = data.get("tool", {}).get("poetry", {})
        poetry_sources = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
        poetry_sources.extend(group.get("dependencies", {}) for group in poetry.get("group", {}).values())
        for source in poetry_sources:
            for dep_name, declaration in source.items():
                if dep_name == "python":
                    continue
                if isinstance(declaration, str):
                    spec = declaration
                    extras = []
                elif isinstance(declaration, dict) and "version" in declaration:
                    if any(key in declaration for key in ("git", "url", "path")):
                        raise ValueError(f"Unsupported Poetry dependency: {dep_name}: {declaration}")
                    spec = declaration["version"]
                    extras = declaration.get("extras", [])
                else:
                    raise ValueError(f"Unresolved Poetry dependency: {dep_name}: {declaration}")
                if spec == "*":
                    spec = ""
                elif spec and spec[0].isdigit():
                    spec = "==" + spec
                if spec.startswith(("^", "~")):
                    raise ValueError(f"Convert Poetry constraint to PEP 508 before audit: {dep_name}{spec}")
                values.append(dep_name + ("[" + ",".join(extras) + "]" if extras else "") + spec)
        return values

    if name == "setup.py":
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assignments = {}
        setup_names = {"setup"}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in {"setuptools", "distutils.core"}:
                setup_names.update(alias.asname or alias.name for alias in node.names if alias.name == "setup")
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assignments[target.id] = node.value

        def evaluate(node, stack=()):
            if isinstance(node, ast.Name):
                if node.id in stack or node.id not in assignments:
                    raise ValueError(f"Unresolved setup.py dependency expression in {path}: {node.id}")
                return evaluate(assignments[node.id], stack + (node.id,))
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                return evaluate(node.left, stack) + evaluate(node.right, stack)
            if isinstance(node, (ast.List, ast.Tuple)):
                return [evaluate(item, stack) for item in node.elts]
            if isinstance(node, ast.Dict):
                return {evaluate(k, stack): evaluate(v, stack) for k, v in zip(node.keys, node.values)}
            try:
                return ast.literal_eval(node)
            except (ValueError, TypeError) as error:
                raise ValueError(f"Unresolved setup.py dependency expression in {path}") from error

        values = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (isinstance(function, ast.Name) and function.id in setup_names
                    or isinstance(function, ast.Attribute) and function.attr == "setup"):
                continue
            for keyword in node.keywords:
                if keyword.arg is None:
                    declarations = evaluate(keyword.value)
                else:
                    if keyword.arg not in {"install_requires", "setup_requires", "tests_require", "requires", "extras_require"}:
                        continue
                    declarations = {keyword.arg: evaluate(keyword.value)}
                for key, deps in declarations.items():
                    if key == "extras_require":
                        for extra_deps in deps.values():
                            values.extend(extra_deps)
                    elif key in {"install_requires", "setup_requires", "tests_require", "requires"}:
                        values.extend(deps)
        if not all(isinstance(value, str) for value in values):
            raise ValueError(f"Non-string dependency in {path}")
        return values
    return requirement_lines(path.read_text(encoding="utf-8"))


def _get_repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        setup_cfg = candidate / "setup.cfg"
        if setup_cfg.exists() and "name = shiv" in setup_cfg.read_text(encoding="utf-8"):
            return candidate
    raise FileNotFoundError("Could not find shiv repo root")


@dataclass
class IsolatedEnv:
    venv_dir: Path
    python: Path
    site_packages: Path


@pytest.fixture(scope="module")
def isolated_envs():
    """Create isolated source (Click 6.7) and target (Click 8.1.7) virtualenvs."""
    repo_root = _get_repo_root()
    base_tmp = Path(tempfile.mkdtemp(prefix="shiv_f2p_"))

    def _make_venv(name: str) -> IsolatedEnv:
        v_dir = base_tmp / name
        subprocess.check_call([sys.executable, "-m", "venv", str(v_dir)], env=isolated_python_env())
        if sys.platform == "win32":
            py = v_dir / "Scripts" / "python.exe"
            sp = v_dir / "Lib" / "site-packages"
        else:
            py = v_dir / "bin" / "python"
            sp = list((v_dir / "lib").glob("python*/site-packages"))[0]
        return IsolatedEnv(venv_dir=v_dir, python=py, site_packages=sp)

    # 1. Source environment: Click 6.7 + baseline source repo from base commit
    src_env = _make_venv("v_src")
    src_repo = base_tmp / "src_repo"
    src_repo.mkdir()

    base_commit = "ff542cbe75ea832df3a989d07c7fdf5214f727fa"
    p_check = run_isolated(["git", "cat-file", "-e", f"{base_commit}^{{commit}}"], cwd=str(repo_root), capture_output=True)
    if p_check.returncode != 0:
        raise RuntimeError(
            f"Base commit {base_commit} cannot be resolved in git repository {repo_root}. "
            "Cannot construct source baseline without the required base commit."
        )

    archive_tar = base_tmp / "base.tar"
    with open(archive_tar, "wb") as f:
        subprocess.check_call(["git", "archive", base_commit], stdout=f, cwd=str(repo_root))
    with tarfile.open(archive_tar) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(path=str(src_repo), filter="data")
        else:
            tar.extractall(path=str(src_repo))

    # Assert source baseline setup.cfg is strictly from base commit and declares Click 6.7
    src_cfg_text = (src_repo / "setup.cfg").read_text(encoding="utf-8")
    assert "click>=6.7,!=7.0" in src_cfg_text, "Source baseline setup.cfg must declare click>=6.7,!=7.0"

    subprocess.check_call([str(src_env.python), "-m", "pip", "install", "click==6.7", "setuptools"], env=isolated_python_env())
    subprocess.check_call([str(src_env.python), "-m", "pip", "install", "--no-deps", "-e", str(src_repo)], env=isolated_python_env())

    # 2. Target environment: Click 8.1.7 + target working tree
    tgt_env = _make_venv("v_tgt")
    subprocess.check_call([str(tgt_env.python), "-m", "pip", "install", "click==8.1.7", "setuptools"], env=isolated_python_env())
    subprocess.check_call([str(tgt_env.python), "-m", "pip", "install", "--no-deps", "-e", str(repo_root)], env=isolated_python_env())

    try:
        yield {"source": src_env, "target": tgt_env, "src_repo": src_repo, "tgt_repo": repo_root}
    finally:
        shutil.rmtree(base_tmp, ignore_errors=True)


def test_f2p_001_setup_cfg_declaration():
    """F2P-001: setup.cfg declares strictly click==8.1.7 without markers or duplicate entries."""
    repo_root = _get_repo_root()
    parser = configparser.ConfigParser()
    parser.read(str(repo_root / "setup.cfg"))
    install_requires = parser.get("options", "install_requires", fallback="").splitlines()

    parsed_reqs = [parse_requirement(line) for line in install_requires]
    click_reqs = [r for r in parsed_reqs if r and r["name"] == "click"]

    assert len(click_reqs) == 1, f"Expected exactly one requirement for 'click', got {click_reqs}"
    req = click_reqs[0]
    assert req["marker"] is None, f"Expected no environment marker, got {req['marker']}"
    assert req["spec"] == "==8.1.7", f"Expected ==8.1.7, got {req['spec']}"


def test_f2p_003_metadata_requires_dist(isolated_envs):
    """F2P-003: In isolated target venv, shiv dist-info METADATA Requires-Dist specifies click == 8.1.7."""
    target_sp = isolated_envs["target"].site_packages
    distributions = [
        d for d in importlib_metadata.distributions(path=[str(target_sp)])
        if normalize_name(d.metadata.get("Name", "")) == "shiv"
    ]
    assert len(distributions) == 1, f"Expected one shiv distribution, found {len(distributions)}"
    click_reqs = [
        Requirement(value)
        for value in (distributions[0].metadata.get_all("Requires-Dist") or [])
        if Requirement(value).name.lower() == "click"
    ]

    assert len(click_reqs) == 1, f"Expected exactly one Requires-Dist for click, got {click_reqs}"
    assert click_reqs[0].marker is None
    assert str(click_reqs[0].specifier) == "==8.1.7"

    # Also contrast with source metadata
    src_dists = [
        d for d in importlib_metadata.distributions(path=[str(isolated_envs["source"].site_packages)])
        if normalize_name(d.metadata.get("Name", "")) == "shiv"
    ]
    assert len(src_dists) == 1
    src_click = [
        Requirement(value)
        for value in (src_dists[0].metadata.get_all("Requires-Dist") or [])
        if Requirement(value).name.lower() == "click"
    ]
    assert len(src_click) == 1
    assert str(src_click[0].specifier) != "==8.1.7"


def test_f2p_004_legacy_click_constraint_cleared(isolated_envs):
    """F2P-004: Systematically scan formal project dependency files and metadata, ensuring click 6.7 is rejected and click==8.1.7 is enforced."""
    tgt_repo = isolated_envs["tgt_repo"]
    target_sp = isolated_envs["target"].site_packages

    # Discover formal dependency sources instead of assuming a fixed file list.
    formal_dependency_files = []
    for path in tgt_repo.rglob("*"):
        relative_parts = path.relative_to(tgt_repo).parts
        if not path.is_file() or any(part in {"test", "tests", ".git", ".venv", "venv", "build", "dist"} for part in relative_parts):
            continue
        name = path.name.lower()
        if name in {"setup.cfg", "setup.py", "pyproject.toml", "tox.ini"} or "requirements" in name or "constraints" in name:
            formal_dependency_files.append(path)
    declarations = [(str(path.relative_to(tgt_repo)), formal_requirements(path))
                    for path in sorted(formal_dependency_files)]
    shiv_distributions = [
        d for d in importlib_metadata.distributions(path=[str(target_sp)])
        if normalize_name(d.metadata.get("Name", "")) == "shiv"
    ]
    assert len(shiv_distributions) == 1, "Expected exactly one installed shiv distribution"
    declarations.append(("installed shiv METADATA", shiv_distributions[0].metadata.get_all("Requires-Dist") or []))

    found_click_req = False
    for origin, raw_requirements in declarations:
        for line in raw_requirements:
            # Invalid or unresolved declarations must fail, never silently disappear.
            requirement = Requirement(line)
            if normalize_name(requirement.name) == "click":
                found_click_req = True
                assert requirement.url is None and requirement.marker is None, f"Conditional or URL Click dependency in {origin}: {line}"
                assert str(requirement.specifier) == "==8.1.7", f"Click requirement in {origin} must be strictly ==8.1.7, got {requirement.specifier}"
                assert "6.7" not in str(requirement), f"Legacy 6.7 constraint found in {origin}: {line}"
                assert "!=7.0" not in str(requirement), f"Legacy !=7.0 constraint found in {origin}: {line}"

    assert found_click_req, "Expected to find at least one formal Click requirement specification in target repository"

    # Verify target setup.cfg via configparser specifically
    cfg = configparser.ConfigParser()
    cfg.read(str(tgt_repo / "setup.cfg"))
    reqs = [Requirement(line) for line in cfg.get("options", "install_requires", fallback="").splitlines() if line.strip()]
    target_click = next(r for r in reqs if r.name.lower() == "click")
    assert str(target_click.specifier) == "==8.1.7"

    # Contrast with source baseline setup.cfg which DID require click>=6.7,!=7.0
    src_cfg = configparser.ConfigParser()
    src_cfg.read(str(isolated_envs["src_repo"] / "setup.cfg"))
    src_reqs = [Requirement(line) for line in src_cfg.get("options", "install_requires", fallback="").splitlines() if line.strip()]
    src_click = next(r for r in src_reqs if r.name.lower() == "click")
    assert src_click.specifier == SpecifierSet(">=6.7,!=7.0")

    # Reusing an include from independent branches is valid; only an include
    # on the current stack is cyclic.
    # The candidate repository is mounted read-only during evaluation.  Use
    # the declared writable build area for transient include fixtures while
    # keeping the paths inside the repository for the parser under test.
    fixture_root = tgt_repo / "build" / "dependency-fixtures"
    fixture_root.mkdir(parents=True, exist_ok=True)
    shared = fixture_root / "requirements-shared.txt"
    branch_a = fixture_root / "requirements-a.txt"
    branch_b = fixture_root / "requirements-b.txt"
    shared.write_text("click==8.1.7\n", encoding="utf-8")
    branch_a.write_text("-r requirements-shared.txt\n", encoding="utf-8")
    branch_b.write_text("-r requirements-shared.txt\n", encoding="utf-8")
    try:
        assert formal_requirements(branch_a) == ["click==8.1.7"]
        assert formal_requirements(branch_b) == ["click==8.1.7"]
    finally:
        shared.unlink()
        branch_a.unlink()
        branch_b.unlink()


def test_p2p_054_isolated_click_environments(isolated_envs):
    """P2P-054: each isolated environment reports the Click it was built with.

    The harness builds two environments the same way before and after the
    migration: a fixed base checkout with Click 6.7 and the candidate tree with
    Click 8.1.7.  This node only checks that each environment is what it claims
    to be, so no comparison can run across a mis-built harness.  Which version
    the candidate *declares* is F2P-001/F2P-003/F2P-004.
    """
    probe_code = "import importlib.metadata; print(importlib.metadata.version('click'))"

    p_src = run_isolated([str(isolated_envs["source"].python), "-c", probe_code], capture_output=True, text=True)
    assert p_src.returncode == 0, p_src.stderr
    assert p_src.stdout.strip() == "6.7", (
        f"the source environment is the fixed base with Click 6.7, got {p_src.stdout.strip()}"
    )

    p_tgt = run_isolated([str(isolated_envs["target"].python), "-c", probe_code], capture_output=True, text=True)
    assert p_tgt.returncode == 0, p_tgt.stderr
    assert p_tgt.stdout.strip() == "8.1.7", (
        f"the target environment is built with Click 8.1.7, got {p_tgt.stdout.strip()}"
    )


def test_p2p_055_imported_click_matches_environment(isolated_envs):
    """P2P-055: an imported Click agrees with the environment it was imported from.

    ``click.__version__``, the installed distribution version and the module
    path must describe the same installation in both environments.
    """
    probe = (
        "import click, importlib.metadata\n"
        "print(click.__version__)\n"
        "print(importlib.metadata.version('click'))\n"
        "print(click.__file__)\n"
    )

    for side, expected in (("target", "8.1.7"), ("source", "6.7")):
        env = isolated_envs[side]
        proc = run_isolated([str(env.python), "-c", probe], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        assert lines[0] == expected, f"{side}: click.__version__ is {lines[0]}, expected {expected}"
        assert lines[1] == expected, f"{side}: installed Click is {lines[1]}, expected {expected}"
        assert str(env.venv_dir).lower() in lines[2].lower(), (
            f"{side}: click must be imported from its own environment, got {lines[2]}"
        )


def test_p2p_056_target_pip_check_clean(isolated_envs):
    """P2P-056: installing the candidate tree leaves the target environment consistent.

    Whatever Click constraint the tree declares, ``pip check`` must report no
    broken requirement after the editable install, because the environment
    already provides Click 8.1.7.
    """
    p_tgt = run_isolated(
        [str(isolated_envs["target"].python), "-m", "pip", "check"],
        capture_output=True,
        text=True,
    )
    assert p_tgt.returncode == 0, f"pip check in target failed: {p_tgt.stdout} {p_tgt.stderr}"
    assert "No broken requirements found" in p_tgt.stdout or "conflict" not in p_tgt.stdout.lower()

#!/usr/bin/env python3
"""Bind the evaluated Go probes to equivalent candidate Go API layouts.

The migration instruction does not prescribe a Go package directory, exported
identifier, or standalone CLI path: it only requires the delivered core to
expose equivalent read-only queries. This module inspects the candidate's Go
source (identifiers only, never executing it) and builds an evaluator-owned
binding package that adapts those identifiers to the public contract the
F2P/DIFFERENTIAL probes assert against. Repository loading and graph
calculation always run inside the candidate's own implementation; an absent or
unbuildable candidate cannot pass.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent.parent  # tests/assets
SHIM_DIRNAME = "seriecore"
SKIP_DIRS = {".git", "vendor", "node_modules", "target", ".gocache", ".gomodcache"}

FUNC_RE = re.compile(
    r"^func\s+(?:\(([^)]*)\)\s*)?([A-Za-z_]\w*)\s*\(([^)]*)\)\s*([^{]*)\{",
    re.M,
)
TYPE_RE = re.compile(r"^type\s+([A-Za-z_]\w*)\s", re.M)


class BindError(RuntimeError):
    pass


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def module_root(repo: Path) -> Path:
    """Locate the root module or the sole nested module without moving the repo.

    Module directories are execution locations only; protected-path and diff
    checks must continue to use the full repository. Missing or ambiguous
    layouts keep the original location so the Go commands still fail closed.
    """

    repo = Path(repo)
    modules: list[Path] = []
    for root, dirs, files in os.walk(repo):
        directory = Path(root)
        dirs[:] = sorted(
            name for name in dirs
            if name not in SKIP_DIRS and name != "testdata"
            and not name.startswith((".", "_"))
            and not (directory / name).is_symlink()
        )
        if "go.mod" not in files or (directory / "go.mod").is_symlink():
            continue
        if directory == repo:
            return repo
        modules.append(directory)
        dirs[:] = []
    return modules[0] if len(modules) == 1 else repo


def module_path(repo: Path) -> str:
    text = _read(repo / "go.mod")
    match = re.search(r"^module\s+(\S+)", text, re.M)
    if not match:
        raise BindError("candidate go.mod has no module declaration")
    return match.group(1)


def _split_types(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    items: list[str] = []
    depth = 0
    current = ""
    for char in text:
        if char in "[(":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "," and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        items.append(current.strip())
    return items


def _param_types(params: str) -> list[str]:
    types: list[str] = []
    for item in _split_types(params):
        if not item:
            continue
        parts = item.split()
        if len(parts) >= 2:
            types.append(" ".join(parts[1:]).strip())
        elif len(parts) == 1:
            types.append(parts[0])
    return types


def _is_variadic_any(params: list[str]) -> bool:
    """Whether a function accepts an arbitrary positional argument list."""

    return params in (["...any"], ["...interface{}"])


def _scan(repo: Path, module: str) -> dict:
    packages: dict[str, dict] = {}
    for root, dirs, files in os.walk(repo):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS and name != SHIM_DIRNAME]
        rel = os.path.relpath(root, repo)
        rel = "." if rel == "." else rel.replace(os.sep, "/")
        pkg = packages.setdefault(
            rel, {"name": "", "funcs": {}, "methods": {}, "types": set(), "type_defs": {}}
        )
        for name in files:
            if not name.endswith(".go") or name.endswith("_test.go"):
                continue
            text = _read(Path(root) / name)
            pkg_name = re.search(r"^package\s+(\w+)", text, re.M)
            if pkg_name:
                pkg["name"] = pkg_name.group(1)
            for match in TYPE_RE.finditer(text):
                pkg["types"].add(match.group(1))
            for match in re.finditer(r"^type\s+(\w+)\s+(?:=\s*)?(\w+)\b", text, re.M):
                pkg["type_defs"][match.group(1)] = match.group(2)
            for match in FUNC_RE.finditer(text):
                receiver, fname, params, results = match.groups()
                ptypes = _param_types(params)
                results = results.strip()
                entry = {"params": ptypes, "results": results}
                if receiver:
                    recv = receiver.split()[-1].lstrip("*").strip()
                    pkg["methods"].setdefault(recv, {})[fname] = entry
                else:
                    pkg["funcs"][fname] = entry
    return packages


def _import_path(module: str, rel: str) -> str:
    return module if rel == "." else module + "/" + rel


def _first_result_type(results: str) -> str:
    results = results.strip()
    if results.startswith("("):
        results = results[1:]
        if results.endswith(")"):
            results = results[:-1]
        return _split_types(results)[0] if _split_types(results) else ""
    return results


def _discover(repo: Path, module: str) -> dict:
    packages = _scan(repo, module)
    best = None
    for rel, pkg in packages.items():
        load_name = None
        for candidate in ("Load", "LoadRepository", "NewLoader", "Open"):
            fn = pkg["funcs"].get(candidate)
            # A loader orchestration helper may take an options struct while
            # the same package also exposes a four-argument repository loader.
            if fn and (len(fn["params"]) == 4 or _is_variadic_any(fn["params"])):
                load_name = candidate
                break
        if not load_name:
            continue
        repo_type = None
        for typ, methods in pkg["methods"].items():
            if "AllCommits" in methods:
                repo_type = typ
                break
        if not repo_type:
            continue
        depth = 0 if rel == "." else rel.count("/") + 1
        score = 0
        if "AllCommits" in pkg["methods"].get(repo_type, {}):
            score += 4
        if any(name in pkg["funcs"] for name in ("CalcGraph", "CalculateGraph")):
            score += 2
        key = (score, -depth)
        if best is None or key > best[0]:
            best = (key, rel, pkg, load_name, repo_type)
    if best is None:
        raise BindError("no candidate Go package exposes Load and AllCommits")
    _, rel, pkg, load_name, repo_type = best
    load = pkg["funcs"][load_name]
    variadic_load = _is_variadic_any(load["params"])
    if len(load["params"]) < 4 and not variadic_load:
        raise BindError("candidate Load does not accept four parameters")
    order_type = "any" if variadic_load else load["params"][1]
    max_type = "any" if variadic_load else load["params"][2]

    calc_rel = None
    calc_name = None
    for cand_rel, cand_pkg in packages.items():
        for name in ("CalcGraph", "CalculateGraph"):
            fn = cand_pkg["funcs"].get(name)
            if not fn or not fn["params"]:
                continue
            param = fn["params"][0].lstrip("*").strip()
            base = param.split(".")[-1]
            if (
                base == repo_type
                or param in ("any", "interface{}")
                or _is_variadic_any(fn["params"])
            ):
                calc_rel, calc_name = cand_rel, name
                break
        if calc_rel:
            break
    if calc_rel is None:
        raise BindError("no candidate CalcGraph accepts the loaded repository type")
    return {
        "module": module,
        "load_rel": rel,
        "load_import": _import_path(module, rel),
        "load_name": load_name,
        "repo_type": repo_type,
        "order_type": order_type,
        "variadic_load": variadic_load,
        "order_is_string": _underlying_type(pkg, order_type) == "string",
        "max_type": max_type,
        "calc_rel": calc_rel,
        "calc_import": _import_path(module, calc_rel),
        "calc_name": calc_name,
        "config_parser": _discover_config_parser(packages, module),
    }


def _discover_config_parser(packages: dict, module: str) -> dict | None:
    """Find an optional public config reader without prescribing its layout."""

    choices = []
    names = ("ParseCoreConfig", "ParseConfig", "Parse", "LoadConfigFromFile", "LoadFile", "LoadConfig")
    for rel, pkg in packages.items():
        if pkg["name"] == "main" or "/internal/" in "/" + rel + "/":
            continue
        for rank, name in enumerate(names):
            fn = pkg["funcs"].get(name)
            if not fn or fn["params"] != ["string"]:
                continue
            result = fn["results"]
            if "error" not in result or "config" not in result.lower():
                continue
            choices.append((rank, rel, name))
    if not choices:
        return None
    _, rel, name = min(choices)
    return {"import": _import_path(module, rel), "name": name,
            "from_file": name.startswith("Load")}


def _underlying_type(pkg: dict, name: str) -> str:
    seen = set()
    while name in pkg["type_defs"] and name not in seen:
        seen.add(name)
        name = pkg["type_defs"][name]
    return name


def _order_expr(info: dict) -> str:
    if info.get("variadic_load"):
        # A flexible public loader cannot expose its private order type in the
        # signature.  The canonical textual value is unambiguous alongside the
        # preceding repository path and is accepted by equivalent variadic
        # orchestrators.
        return "order.String()"
    order_type = info["order_type"].strip()
    if order_type in ("any", "interface{}"):
        # A candidate that accepts any ordering value receives the canonical
        # integer so it can map it to its own order type without knowing the
        # evaluator's type.
        return "int(order)"
    value = "order.String()" if info.get("order_is_string") else "order"
    if order_type in ("string", "int", "int32", "int64", "uint", "uint32", "uint64"):
        return "%s(%s)" % (order_type, value)
    return "real.%s(%s)" % (order_type, value)


def _max_expr(info: dict) -> tuple[str, bool]:
    max_type = info["max_type"].strip()
    if max_type in ("*int", "any", "interface{}"):
        return "maxCount", False
    if max_type == "int":
        return "maxCountValue(maxCount)", True
    return "maxCount", False


def _bootstrap(info: dict) -> str:
    need_helper = False
    order_expr = _order_expr(info)
    max_expr, need_helper = _max_expr(info)
    lines = ["package core", ""]
    lines.append('import real "%s"' % info["load_import"])
    calc_qualifier = "real"
    if info["calc_import"] != info["load_import"]:
        lines.append('import calcpkg "%s"' % info["calc_import"])
        calc_qualifier = "calcpkg"
    config = info.get("config_parser")
    config_qualifier = "real"
    if config and config["import"] != info["load_import"]:
        if config["import"] == info["calc_import"]:
            config_qualifier = calc_qualifier
        else:
            lines.append('import configpkg "%s"' % config["import"])
            config_qualifier = "configpkg"
    if config and config["from_file"]:
        lines.append('import "os"')
    lines.append("")
    if need_helper:
        lines.append("func maxCountValue(maxCount *int) int {")
        lines.append("\tif maxCount == nil {\n\t\treturn 0\n\t}")
        lines.append("\treturn *maxCount")
        lines.append("}")
        lines.append("")
    lines.append("// realLoad delegates repository loading to the candidate core.")
    lines.append("func realLoad(path string, order SortCommit, maxCount *int, mailmap bool) (any, error) {")
    lines.append("\treturn real.%s(path, %s, %s, mailmap)" % (info["load_name"], order_expr, max_expr))
    lines.append("}")
    lines.append("")
    lines.append("// realCalcGraph delegates graph calculation to the candidate core.")
    lines.append("func realCalcGraph(repo any) any {")
    lines.append("\treturn %s.%s(repo.(*real.%s))" % (calc_qualifier, info["calc_name"], info["repo_type"]))
    lines.append("}")
    lines.append("")
    lines.append("const candidateCoreConfigParser = %s" % ("true" if config else "false"))
    lines.append("func realParseCoreConfig(content string) (any, bool, error) {")
    if not config:
        lines.append("\treturn nil, false, nil")
    else:
        argument = "content"
        if config["from_file"]:
            lines.extend([
                '\tfile, err := os.CreateTemp("", "serie-config-*.toml")',
                '\tif err != nil { return nil, true, err }',
                '\tdefer os.Remove(file.Name())',
                '\tif _, err = file.WriteString(content); err != nil { file.Close(); return nil, true, err }',
                '\tif err = file.Close(); err != nil { return nil, true, err }',
            ])
            argument = "file.Name()"
        lines.append("\tvalue, err := %s.%s(%s)" % (config_qualifier, config["name"], argument))
        lines.append("\treturn value, true, err")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _cli_main(module: str) -> str:
    text = _read(HERE / "serie_contract_cli.go.txt")
    return text.replace('"github.com/lusingander/serie/core"', '"%s/%s"' % (module, SHIM_DIRNAME))


def _probe_source(module: str) -> str:
    text = _read(ASSETS / "shared" / "batches" / "BATCH-F2P-0001" / "probe.go")
    return text.replace('"github.com/lusingander/serie/core"', '"%s/%s"' % (module, SHIM_DIRNAME))


def shim_dir(contract: Path) -> Path:
    return contract / SHIM_DIRNAME


def build_contract_repo(repo: Path) -> Path:
    """Return a copy of the candidate repo with the evaluator binding added."""

    repo = module_root(Path(repo))
    info = _discover(repo, module_path(repo))
    root = Path(tempfile.mkdtemp(prefix="serie-contract-"))
    contract = root / "repo"
    shutil.copytree(
        repo,
        contract,
        ignore=shutil.ignore_patterns(".git", ".gocache", ".gomodcache", "vendor", "node_modules"),
        symlinks=True,
    )
    shim = shim_dir(contract)
    if shim.exists():
        shutil.rmtree(shim)
    shim.mkdir()
    (shim / "zz_contract_core.go").write_text(
        _read(HERE / "serie_contract_core.go.txt"), encoding="utf-8"
    )
    (shim / "zz_bootstrap.go").write_text(_bootstrap(info), encoding="utf-8")

    cli = contract / "cmd" / "serie-core"
    if cli.exists():
        shutil.rmtree(cli)
    cli.mkdir(parents=True)
    (cli / "main.go").write_text(_cli_main(info["module"]), encoding="utf-8")

    # A standalone probe module is kept outside the evaluated module so its
    # source is never part of the candidate's protected/forbidden-mechanism scan.
    probe_root = root / "probe"
    probe_root.mkdir()
    (probe_root / "probe.go").write_text(_probe_source(info["module"]), encoding="utf-8")
    (probe_root / "go.mod").write_text(
        "module serie-f2p-probe\n\ngo 1.24.0\n\nrequire %s v0.0.0\n\nreplace %s => %s\n"
        % (info["module"], info["module"], contract),
        encoding="utf-8",
    )
    info["root"] = str(root)
    info["contract"] = str(contract)
    info["probe_root"] = str(probe_root)
    return contract


def probe_binary(contract: Path) -> Path | None:
    """Build the standalone F2P-0001 probe for a contract workspace."""

    contract = Path(contract)
    probe_root = contract.parent / "probe"
    if not (probe_root / "probe.go").is_file():
        return None
    binary = probe_root / "serie-probe"
    env = dict(os.environ)
    env.update(
        {
            "CGO_ENABLED": "0",
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "GOTOOLCHAIN": "local",
            "GOFLAGS": "",
        }
    )
    completed = subprocess.run(
        ["go", "build", "-buildvcs=false", "-o", str(binary), "."],
        cwd=str(probe_root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env=env,
    )
    if completed.returncode != 0 or not binary.is_file():
        return None
    return binary


def write_test(contract: Path, name: str, source_asset: Path) -> Path:
    target = shim_dir(contract) / name
    target.write_text(_read(source_asset), encoding="utf-8")
    return target


def clear_test(contract: Path, name: str) -> None:
    target = shim_dir(contract) / name
    if target.exists():
        target.unlink()

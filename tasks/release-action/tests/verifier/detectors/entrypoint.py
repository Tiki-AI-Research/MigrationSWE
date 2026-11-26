"""Source-runtime evidence from the real action entry chain."""
import importlib.util
from pathlib import Path
import sys

from .runtime_identity import source_language


def detect_entrypoint_source_runtime(root, config):
    if source_language(config) not in {"typescript", "javascript"}:
        return []
    action = root / "action.yml"
    if not action.is_file() or action.is_symlink():
        return []
    key = "_release_action_launch_inspector"
    module = sys.modules.get(key)
    if module is None:
        path = Path(__file__).resolve().parents[2] / "assets/shared/integration/launch.py"
        spec = importlib.util.spec_from_file_location(key, path, submodule_search_locations=[str(path.parent)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return [{"detector": "entrypoint", "capability": "callgraph", **hit}
            for hit in module.inspect_action(root, action.read_text())["source_runtime_hits"]]

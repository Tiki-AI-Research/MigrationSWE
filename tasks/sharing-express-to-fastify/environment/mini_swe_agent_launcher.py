"""Preserve gateway model capabilities when Harbor supplies its own registry.

Run with Python -S so Harbor's sitecustomize/task-file hook runs exactly once,
in the real agent process. No request parameters or task arguments are changed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile


AGENT = "/opt/mini-swe-agent/bin/mini-swe-agent"
REGISTRY_OPTION = "model.litellm_model_registry="
REASONING_MODELS = frozenset({"openai/gemini-4-argon", "openai/muse-wonder"})


def complete_registry(registry):
    if not isinstance(registry, dict):
        raise ValueError("Mini-SWE model registry must be a JSON object")
    result = dict(registry)
    for model in REASONING_MODELS.intersection(registry):
        entry = registry[model]
        if not isinstance(entry, dict):
            raise ValueError(f"Mini-SWE model registry entry must be an object: {model}")
        # Preserve explicit capability decisions and every other registry field.
        # This fills the capability missing from Harbor's streaming registry.
        result[model] = {**entry, "supports_reasoning": entry.get("supports_reasoning", True)}
    return result


def prepare_arguments(arguments, *, directory=None):
    result = list(arguments)
    for index in range(1, len(result)):
        if result[index - 1] not in {"-c", "--config"}:
            continue
        specification = result[index]
        if not specification.startswith(REGISTRY_OPTION):
            continue
        source = Path(specification[len(REGISTRY_OPTION):])
        registry = json.loads(source.read_text(encoding="utf-8"))
        completed = complete_registry(registry)
        if completed == registry:
            continue
        # Keep the executor's original registry intact. The new file lives only
        # in this disposable agent container, outside the candidate repository.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="sharing-model-registry-",
            suffix=".json", dir=directory, delete=False,
        ) as output:
            json.dump(completed, output, ensure_ascii=False, indent=2)
            output.write("\n")
            result[index] = REGISTRY_OPTION + output.name
    return result


def main():
    arguments = prepare_arguments(sys.argv[1:])
    # Use the hash-locked environment already built into this task image.
    # Harbor's separate uv bootstrap must not select a newer tool environment.
    os.execv(AGENT, [AGENT, *arguments])


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"sharing agent launcher: {error}", file=sys.stderr)
        raise SystemExit(1) from error

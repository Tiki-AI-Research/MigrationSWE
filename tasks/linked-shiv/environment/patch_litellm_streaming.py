"""Fix the pinned agent SDK's reasoning-text field without changing requests.

LiteLLM 1.80.0 requires ``reasoning`` in a reasoning_text content-part event,
but the streaming endpoint supplies the same text in ``text``. Accept both
spellings before Pydantic validates the event, preserving the full text for
legacy consumers of the ``reasoning`` attribute.
"""

import ast
import importlib.util
from pathlib import Path


IMPORT = (
    "from pydantic import AliasChoices as _HarborReasoningAliases, "
    "Field as _HarborReasoningField\n"
)
FIELD = (
    '    reasoning: str = _HarborReasoningField(validation_alias='
    '_HarborReasoningAliases("reasoning", "text"))\n'
)


def patch_source(source: str) -> str:
    """Patch only the known faulty field; fail the build on unexpected drift."""
    classes = [
        node for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef)
        and node.name == "ContentPartDonePartReasoningText"
    ]
    if len(classes) != 1:
        raise RuntimeError("Expected one LiteLLM reasoning-text content-part class")
    cls = classes[0]
    fields = [
        node for node in cls.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "reasoning"
    ]
    if len(fields) != 1:
        raise RuntimeError("Unexpected LiteLLM reasoning-text field layout")
    field = fields[0]
    lines = source.splitlines(keepends=True)
    if lines[field.lineno - 1] == FIELD and IMPORT in source:
        return source
    if (
        field.value is not None
        or not isinstance(field.annotation, ast.Name)
        or field.annotation.id != "str"
        or field.lineno != field.end_lineno
    ):
        raise RuntimeError("LiteLLM reasoning-text field changed; review the patch")
    lines[field.lineno - 1] = FIELD
    lines.insert(cls.lineno - 1, IMPORT + "\n")
    patched = "".join(lines)
    ast.parse(patched)
    return patched


def verify_event_types(event_type, validation_error) -> None:
    """Exercise the failing event and keep ordinary and invalid events strict."""
    envelope = {
        "type": "response.content_part.done",
        "item_id": "reasoning-smoke-test",
        "output_index": 0,
        "content_index": 0,
    }
    for key in ("text", "reasoning"):
        event = event_type(**envelope, part={"type": "reasoning_text", key: "\u5b8c\u6574\u63a8\u7406\n"})
        assert event.part.reasoning == "\u5b8c\u6574\u63a8\u7406\n"
    for part in (
        {"type": "output_text", "text": "output", "annotations": [], "logprobs": []},
        {"type": "refusal", "refusal": "refused"},
    ):
        assert event_type(**envelope, part=part).part.model_dump() == part
    for part in ({"type": "reasoning_text"}, {"type": "reasoning_text", "text": None}):
        try:
            event_type(**envelope, part=part)
        except validation_error:
            pass
        else:
            raise AssertionError("Malformed reasoning events must still fail validation")


def main() -> None:
    spec = importlib.util.find_spec("litellm")
    if spec is None or spec.origin is None:
        raise RuntimeError("Run with the preinstalled mini-swe-agent interpreter")
    source_path = Path(spec.origin).parent / "types" / "llms" / "openai.py"
    source = source_path.read_text(encoding="utf-8")
    patched = patch_source(source)
    if patched != source:
        source_path.write_text(patched, encoding="utf-8")

    from litellm.types.llms.openai import ContentPartDoneEvent
    from pydantic import ValidationError

    verify_event_types(ContentPartDoneEvent, ValidationError)
    print("mini-swe-agent: LiteLLM reasoning-text streaming compatibility verified")


if __name__ == "__main__":
    main()

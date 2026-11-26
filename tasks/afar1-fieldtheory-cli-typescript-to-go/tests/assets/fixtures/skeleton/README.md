# Fixtures skeleton

Deterministic, self-contained inputs shared by the source and target probes
(JSONL bookmark caches, `.env` files, minimal command documents, and engine stub
scripts). They are added by later authoring batches; `BATCH-SKELETON-0001` only
reserves the directory.

## Conventions

- Fixtures MUST be deterministic and MUST NOT require network access, root
  privileges, or interactivity.
- Any engine stub used to exercise the LLM bridge MUST terminate on its own
  (bounded work) so that a probe never blocks indefinitely.

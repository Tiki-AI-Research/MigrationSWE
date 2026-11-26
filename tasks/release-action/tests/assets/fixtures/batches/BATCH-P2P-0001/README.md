# BATCH-P2P-0001 fixtures

`p2p_cases.json` is the declarative input/expected corpus shared by the source
and target P2P probes. It mirrors the pre-migration TypeScript test inputs and
the expected observable (normalized) behaviour so both sides can be compared.

Human-readable messages are only compared for the GitHub error rendering, whose
exact strings the instruction names as public contract; every other case
compares existence, structure, ordering, status/type/code, or normalized
numeric/boolean values.

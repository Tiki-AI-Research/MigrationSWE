# BATCH-DIFFERENTIAL-0001 target probe

Builds the post-migration Go implementation (`/input/target` by default,
overridable with `TARGET_REPO`) and runs the shared case corpus against the
compiled binary.

The build copies the Go sources to a writable temporary directory (the reference
tree is read-only), reuses the mounted module cache, and runs under an explicit
300 second timeout. Each case points `GITHUB_API_URL` at a recording mock GitHub
server and runs under an explicit 60 second timeout.

```
python3 target/batches/BATCH-DIFFERENTIAL-0001/probe.py /path/to/report.json
```

The probe was executed against the reference target while authoring; its
normalized observations are captured in
`fixtures/batches/BATCH-DIFFERENTIAL-0001/expected.json`.

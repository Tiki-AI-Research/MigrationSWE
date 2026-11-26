# BATCH-DIFFERENTIAL-0001 shared assets

Differential assets for the TypeScript 6.0.3 -> Go 1.23.0 migration of
`ncipollo/release-action`. The same case corpus is executed unchanged against
the pre-migration implementation (`/input/source`) and the migrated
implementation (`/input/target`); every observation must match after
normalization.

## Layout

- `corpus/cases.json` - the shared declarative case corpus. Each case declares
  the `INPUT_*` environment, the workflow context, files to materialize, the
  programmable GitHub REST responses, and the observations it covers. Release
  bodies use `{base}` for the mock server origin.
- `lib/harness.py` - the shared runner: a recording mock GitHub server, a
  timeout-bounded subprocess runner, output parsing, and normalization.
- `lib/compare.py` - structural equality between the source and target reports.
- `lib/check_fragment.py` - authoring self-check that validates the fragment and
  the calibratability of every residue rule.

## Observable projection

Only these are compared: process exit code, the ordered GitHub REST requests
(method, path, parsed JSON body or upload metadata), the action outputs written
to `GITHUB_OUTPUT`, and the whitespace-normalized `::error::` diagnostic.
Human-readable log text never participates. `upload_url` values are normalized
to `{base}` so the ephemeral mock port is not observable.

## Timeouts

Every potentially long command is bounded explicitly: the Go build runs under a
300 second timeout and each case subprocess under a 60 second timeout. A
timeout is reported as exit code `124`; the harness can never wait forever.

## Harness entry points

`source/runner` and `target/runner` are created only by the INTEGRATION batch.
The probes here are wired underneath those entry points; the INTEGRATION runner
invokes `source/batches/BATCH-DIFFERENTIAL-0001/probe.py` and
`target/batches/BATCH-DIFFERENTIAL-0001/probe.py` and then applies
`lib/compare.py`.

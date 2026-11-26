#!/bin/sh
# Shared helpers for the BATCH-P2P-0002 preserve-to-preserve (P2P) probes.
#
# Every potentially long-running command (npm test, go test, go build, ...) MUST
# be routed through run_timed/run_default/run_capture so the harness can never
# block indefinitely. Interactive TTY prompts, watchers, network access and
# privileged operations MUST NOT be invoked from these probes; all child
# processes get stdin redirected from /dev/null.

set -eu

# Wall-clock budget for a repository test command, in seconds.
: "${FT_P2P_TEST_TIMEOUT:=300}"
# Wall-clock budget for a single binary smoke invocation, in seconds.
: "${FT_P2P_SMOKE_TIMEOUT:=60}"
# Grace period between SIGTERM and SIGKILL, in seconds.
: "${FT_P2P_KILL_AFTER:=5}"

# run_timed <seconds> <command> [args...]
run_timed() {
  _ft_seconds="$1"
  shift
  timeout -k "${FT_P2P_KILL_AFTER}" "${_ft_seconds}" "$@" </dev/null
}

# run_default <command> [args...]
run_default() {
  run_timed "${FT_P2P_TEST_TIMEOUT}" "$@"
}

# p2p_asset_root - root of the authored asset tree (contains shared/, fixtures/).
# The probe lives at <root>/<side>/batches/BATCH-P2P-0002/probe.sh.
p2p_asset_root() {
  _ft_here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
  CDPATH= cd -- "${_ft_here}/../../.." && pwd
}

# p2p_fixtures_dir - deterministic fixture directory for this batch.
p2p_fixtures_dir() {
  printf '%s/fixtures/batches/BATCH-P2P-0002\n' "$(p2p_asset_root)"
}

# p2p_source_root - the TypeScript source repository for this probe.
#   FT_REPO wins; otherwise the first candidate that looks like the source
#   checkout (package.json + src/) is used.
p2p_source_root() {
  if [ -n "${FT_REPO:-}" ]; then
    printf '%s\n' "${FT_REPO}"
    return 0
  fi
  for _ft_dir in /workspace/repo /input/source; do
    if [ -f "${_ft_dir}/package.json" ] && [ -d "${_ft_dir}/src" ]; then
      printf '%s\n' "${_ft_dir}"
      return 0
    fi
  done
  printf '%s\n' "/workspace/repo"
}

# p2p_target_root - the Go reference target repository for this probe.
#   FT_REPO wins; otherwise the first candidate that looks like the target
#   checkout (go.mod + cmd/) is used.
p2p_target_root() {
  if [ -n "${FT_REPO:-}" ]; then
    printf '%s\n' "${FT_REPO}"
    return 0
  fi
  for _ft_dir in /workspace/repo /input/target; do
    if [ -f "${_ft_dir}/go.mod" ] && [ -d "${_ft_dir}/cmd" ]; then
      printf '%s\n' "${_ft_dir}"
      return 0
    fi
  done
  printf '%s\n' "/workspace/repo"
}

# p2p_observations - the observation IDs owned by this batch, in order.
p2p_observations() {
  cat <<'OBS'
P2P-025
P2P-026
P2P-027
P2P-028
P2P-029
P2P-030
P2P-031
P2P-032
P2P-033
P2P-034
P2P-035
P2P-036
P2P-037
P2P-038
P2P-039
P2P-040
OBS
}

# p2p_status_for <observation_id> - prints the raw side status for one
#   observation. The default is the batch-wide status ($P2P_STATUS); probes may
#   override this function when a dedicated signal exists for an observation.
p2p_status_for() {
  printf '%s\n' "${P2P_STATUS:-INCONCLUSIVE}"
}

# p2p_emit_jsonl - writes one normalized projection record per observation to
#   stdout (line-delimited JSON, stream friendly).
p2p_emit_jsonl() {
  while IFS= read -r _ft_obs; do
    _ft_st=$(p2p_status_for "${_ft_obs}")
    printf '{"observation_id":"%s","value":{"result":"%s"}}\n' "${_ft_obs}" "${_ft_st}"
  done <<OBS
$(p2p_observations)
OBS
}

# p2p_write_report <path> - writes a single valid JSON document covering every
#   observation ID to <path>. The runner consumes the file named by the runtime
#   {report} placeholder, which is a `.json` document (not JSONL).
p2p_write_report() {
  _ft_path="$1"
  _ft_first=1
  {
    printf '{"batch_id":"BATCH-P2P-0002","side":"%s","observations":[' "${P2P_SIDE:-unknown}"
    while IFS= read -r _ft_obs; do
      _ft_st=$(p2p_status_for "${_ft_obs}")
      if [ "${_ft_first}" -eq 0 ]; then
        printf ','
      fi
      _ft_first=0
      printf '{"observation_id":"%s","value":{"result":"%s"}}' "${_ft_obs}" "${_ft_st}"
    done <<OBS
$(p2p_observations)
OBS
    printf ']}\n'
  } >"${_ft_path}"
}

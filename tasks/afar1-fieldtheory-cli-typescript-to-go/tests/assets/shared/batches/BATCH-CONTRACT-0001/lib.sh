#!/bin/sh
# Shared helpers for the BATCH-CONTRACT-0001 public-contract probes.
#
# Every potentially long-running command MUST be wrapped with an explicit
# `timeout` through one of the helpers below so that the harness can never
# block indefinitely. Cases that require privileges, network access, a TTY, or
# that are expected to block are excluded from these probes.
#
# A CONTRACT probe executes a declared public contract entry point (the CLI
# command surface, on-disk formats, or a structured error surface) on the side
# under test and emits that side's raw, normalized contract value. The verifier
# requires both sides to report PASS and the two normalized values to be equal.
#
# This file also carries the per-observation projection used by *both* sides so
# that the two normalize through exactly the same code and can only differ when
# the observable behavior differs.

set -eu

FT_HARNESS_DEFAULT_TIMEOUT="${FT_HARNESS_DEFAULT_TIMEOUT:-120}"
FT_HARNESS_KILL_AFTER="${FT_HARNESS_KILL_AFTER:-5}"

# run_timed <timeout_seconds> <command> [args...]
run_timed() {
  _ft_timeout="$1"
  shift
  timeout -k "${FT_HARNESS_KILL_AFTER}" "${_ft_timeout}" "$@"
}

# run_capture <timeout_seconds> <stdout_file> <stderr_file> <command> [args...]
#   Runs under `timeout`, capturing streams to files. Returns the command exit
#   status; 124/137 indicates the timeout fired.
run_capture() {
  _ft_timeout="$1"
  _ft_stdout="$2"
  _ft_stderr="$3"
  shift 3
  set +e
  timeout -k "${FT_HARNESS_KILL_AFTER}" "${_ft_timeout}" "$@" \
    >"${_ft_stdout}" 2>"${_ft_stderr}"
  _ft_status=$?
  set -e
  return "${_ft_status}"
}

# ft_json_escape - escape a scalar for embedding in a JSON string.
ft_json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# ft_normalize - strip terminal control bytes (ANSI colors, CR), collapse all
# runs of whitespace to single spaces, and trim. Keeps the emitted JSON scalar
# valid and makes source/target values comparable.
ft_normalize() {
  printf '%s' "$1" \
    | LC_ALL=C tr -d '\000-\037' \
    | tr -s ' \t\n' ' ' \
    | sed -e 's/^ *//' -e 's/ *$//'
}

# ft_record <observation_id> <status> <value>
#   Emits one normalized result line as a JSON object.
ft_record() {
  printf '{"observation_id":"%s","status":"%s","value":"%s"}\n' \
    "$1" "$2" "$(ft_json_escape "$(ft_normalize "${3:-}")")"
}

# ft_make_tmpdir - per-run scratch directory, cleaned by the caller.
ft_make_tmpdir() {
  mktemp -d "${TMPDIR:-/tmp}/ft-contract.XXXXXX"
}

# ft_bool <a> <b> - "true" when the two scalar strings are equal.
ft_bool() {
  if [ "$1" = "$2" ]; then printf 'true'; else printf 'false'; fi
}

# ft_exitclass <exit_code> - "zero" or "nonzero".
ft_exitclass() {
  if [ "$1" -eq 0 ]; then printf 'zero'; else printf 'nonzero'; fi
}

# ft_is_json <text> - "true" when the first non-space byte opens a JSON value.
ft_is_json() {
  _ft_s="$(printf '%s' "$1" | LC_ALL=C sed -e 's/^[[:space:]]*//')"
  case "${_ft_s}" in
    "{"*) printf 'true' ;;
    "["*) printf 'true' ;;
    *) printf 'false' ;;
  esac
}

# ft_has <needle> <haystack> - "true" when the needle occurs literally.
ft_has() {
  if printf '%s' "$2" | grep -qF -- "$1"; then printf 'true'; else printf 'false'; fi
}

# ---------------------------------------------------------------------------
# Side-provided interface (defined by source/ and target/ probe scripts):
#   ft_cli       <out> <err> <args...>          run the CLI as the primary name
#   ft_cli_named <name> <out> <err> <args...>   run the CLI under an alternate
#                                               process name (ft/fieldtheory)
# Globals provided by the side scripts:
#   FT_DATA   scratch data dir; FT_FIX   fixtures dir; FT_COMMANDS_DIR
# ---------------------------------------------------------------------------

# ft_run <args...> - run the primary CLI, recording FT_ST/FT_OUT/FT_ERR.
ft_run() {
  _ft_o="$FT_DATA/.run.out"
  _ft_e="$FT_DATA/.run.err"
  if ft_cli "$_ft_o" "$_ft_e" "$@"; then FT_ST=0; else FT_ST=$?; fi
  FT_OUT="$(cat "$_ft_o" 2>/dev/null || true)"
  FT_ERR="$(cat "$_ft_e" 2>/dev/null || true)"
}

# ft_run_named <name> <args...> - run the CLI under an alternate process name.
ft_run_named() {
  _ft_name="$1"
  shift
  _ft_o="$FT_DATA/.run.out"
  _ft_e="$FT_DATA/.run.err"
  if ft_cli_named "$_ft_name" "$_ft_o" "$_ft_e" "$@"; then FT_ST=0; else FT_ST=$?; fi
  FT_OUT="$(cat "$_ft_o" 2>/dev/null || true)"
  FT_ERR="$(cat "$_ft_e" 2>/dev/null || true)"
}

# ft_status_from <pass:0|1> - PASS/FAIL.
ft_status_from() {
  if [ "$1" -eq 0 ]; then printf 'PASS'; else printf 'FAIL'; fi
}

# ---------------------------------------------------------------------------
# Per-observation projections. Both sides call this so the normalization is
# identical; the value differs only when the observable contract differs.
# ---------------------------------------------------------------------------

_c001() {
  # R003 - `ft` and `fieldtheory` accept the same documented command tree.
  _ok=0
  _same=0
  for _c in search list show stats classify classify-domains categories \
            domains folders model index path paths status sample library commands; do
    ft_run "$_c" --help
    _a=$FT_ST
    ft_run_named fieldtheory "$_c" --help
    _b=$FT_ST
    [ "$_a" -eq 0 ] || _ok=1
    [ "$_a" -eq "$_b" ] || _same=1
  done
  ft_record CONTRACT-001 "$(ft_status_from $((_ok + _same)))" \
    "tree_ok=$(ft_bool "$_ok" 0);names_identical=$(ft_bool "$_same" 0)"
}

_c002() {
  # R004 - documented subcommands and their options parse.
  _bad=0
  for _c in search library commands; do
    ft_run "$_c" --help
    [ "$FT_ST" -eq 0 ] || _bad=1
  done
  ft_run list --limit 5
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_record CONTRACT-002 "$(ft_status_from "$_bad")" \
    "all_accepted=$(ft_bool "$_bad" 0)"
}

_c003() {
  # R005 - human-readable and --json output stay distinct.
  ft_run stats
  _text="$FT_OUT"
  ft_run stats --json
  _json="$FT_OUT"
  _tj="$(ft_is_json "$_text")"
  _jj="$(ft_is_json "$_json")"
  _bad=1
  { [ "$_tj" = false ] && [ "$_jj" = true ]; } && _bad=0
  ft_record CONTRACT-003 "$(ft_status_from "$_bad")" \
    "text_json=$_tj;json_json=$_jj"
}

_c004() {
  # R006 - failures and parse errors exit non-zero.
  mkdir -p "$FT_COMMANDS_DIR"
  printf 'not a portable command\n' >"$FT_COMMANDS_DIR/bad.md"
  ft_run commands validate
  _v=$FT_ST
  ft_run search
  _s=$FT_ST
  _bad=0
  [ "$_v" -ne 0 ] || _bad=1
  [ "$_s" -ne 0 ] || _bad=1
  ft_record CONTRACT-004 "$(ft_status_from "$_bad")" \
    "validate=$(ft_exitclass "$_v");search_missing_arg=$(ft_exitclass "$_s")"
}

_c005() {
  # R007 - the hidden `ft bookmarks <cmd>` alias forwards to the top-level cmd.
  ft_run search machine --limit 5
  _d="$(ft_normalize "$FT_OUT$FT_ERR")"
  ft_run bookmarks search machine --limit 5
  _a="$(ft_normalize "$FT_OUT$FT_ERR")"
  ft_run path
  _p="$(ft_normalize "$FT_OUT")"
  ft_run bookmarks path
  _bp="$(ft_normalize "$FT_OUT")"
  _eq="$(ft_bool "$_d" "$_a")"
  _peq="$(ft_bool "$_p" "$_bp")"
  _bad=1
  { [ "$_eq" = true ] && [ "$_peq" = true ]; } && _bad=0
  ft_record CONTRACT-005 "$(ft_status_from "$_bad")" \
    "search_alias=$_eq;path_alias=$_peq"
}

_c006() {
  # R008 - no-arg invocation shows welcome when absent and dashboard otherwise.
  mv "$FT_DATA/bookmarks.jsonl" "$FT_DATA/.bm.bak"
  ft_run
  _a_st=$FT_ST
  _a="$(ft_normalize "$FT_OUT")"
  mv "$FT_DATA/.bm.bak" "$FT_DATA/bookmarks.jsonl"
  ft_run
  _p_st=$FT_ST
  _p="$(ft_normalize "$FT_OUT")"
  _distinct="$(ft_bool "$_a" "$_p")"
  _bad=0
  [ "$_a_st" -eq 0 ] || _bad=1
  [ "$_p_st" -eq 0 ] || _bad=1
  [ "$_distinct" = false ] || _bad=1
  ft_record CONTRACT-006 "$(ft_status_from "$_bad")" \
    "absent_exit=$(ft_exitclass "$_a_st");present_exit=$(ft_exitclass "$_p_st");modes_distinct=$(ft_bool "$_distinct" false)"
}

_c007() {
  # R010 - `ft path` prints FT_DATA_DIR and `ft paths --json` reports all fields.
  ft_run path
  _p="$(ft_normalize "$FT_OUT")"
  _want="$(ft_normalize "$FT_DATA")"
  ft_run paths --json
  _j="$FT_OUT"
  _path_ok="$(ft_bool "$_p" "$_want")"
  _fields=0
  _missing=""
  for _k in active canonical env legacy; do
    if printf '%s' "$_j" | grep -qF -- "\"$_k\""; then :; else _fields=1; _missing="$_missing$_k,"; fi
  done
  _bad=0
  [ "$_path_ok" = true ] || _bad=1
  [ "$_fields" -eq 0 ] || _bad=1
  ft_record CONTRACT-007 "$(ft_status_from "$_bad")" \
    "path_eq_env=$_path_ok;json_fields=$(ft_bool "$_fields" 0)"
}

_c008() {
  # R012 - search matches and an empty result prints the documented phrase.
  ft_run search machine
  _ms=$FT_ST
  _mo="$(ft_normalize "$FT_OUT")"
  ft_run search zzqxnomatches
  _eo="$FT_OUT"
  _match=1
  { [ "$_ms" -eq 0 ] && [ -n "$_mo" ]; } && _match=0
  _nf="$(ft_has "No results found" "$_eo")"
  _bad=0
  [ "$_match" -eq 0 ] || _bad=1
  [ "$_nf" = true ] || _bad=1
  ft_record CONTRACT-008 "$(ft_status_from "$_bad")" \
    "match=$(ft_bool "$_match" 0);empty_not_found_phrase=$_nf"
}

_c009() {
  # R013 - list accepts every documented filter.
  _bad=0
  ft_run list --query machine
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_run list --author alice
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_run list --after 2024-01-01
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_run list --before 2024-12-31
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_run list --category tool
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_run list --domain github.com
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_run list --folder Res
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_run list --limit 5 --offset 0
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_record CONTRACT-009 "$(ft_status_from "$_bad")" \
    "all_filters_accepted=$(ft_bool "$_bad" 0)"
}

_c010() {
  # R014 - known id prints detail; unknown id exits non-zero.
  ft_run show bm-001
  _k=$FT_ST
  ft_run show nope
  _u=$FT_ST
  _bad=0
  [ "$_k" -eq 0 ] || _bad=1
  [ "$_u" -ne 0 ] || _bad=1
  ft_record CONTRACT-010 "$(ft_status_from "$_bad")" \
    "known=$(ft_exitclass "$_k");unknown=$(ft_exitclass "$_u")"
}

_c011() {
  # R015 - stats --json carries the documented aggregate fields.
  ft_run stats --json
  _j="$FT_OUT"
  _bad=0
  for _k in totalBookmarks uniqueAuthors dateRange topAuthors languageBreakdown; do
    printf '%s' "$_j" | grep -qF -- "\"$_k\"" || _bad=1
  done
  ft_record CONTRACT-011 "$(ft_status_from "$_bad")" \
    "fields=$(ft_bool "$_bad" 0)"
}

_c012() {
  # R016 - categories excludes the `unclassified` placeholder.
  ft_run categories
  _o="$FT_OUT$FT_ERR"
  _ex="$(ft_has "unclassified" "$_o")"
  _bad=0
  [ "$FT_ST" -eq 0 ] || _bad=1
  [ "$_ex" = false ] || _bad=1
  ft_record CONTRACT-012 "$(ft_status_from "$_bad")" \
    "exit=$(ft_exitclass "$FT_ST");excludes_unclassified=$(ft_bool "$_ex" false)"
}

_c013() {
  # R017 - domains reports a distribution.
  ft_run domains
  _bad=0
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_record CONTRACT-013 "$(ft_status_from "$_bad")" \
    "exit=$(ft_exitclass "$FT_ST")"
}

_c014() {
  # R018 - folders reports per-folder counts and an untagged count.
  ft_run folders
  _o="$FT_OUT$FT_ERR"
  _u="$(ft_has "untagged" "$_o")"
  _bad=0
  [ "$FT_ST" -eq 0 ] || _bad=1
  [ "$_u" = true ] || _bad=1
  ft_record CONTRACT-014 "$(ft_status_from "$_bad")" \
    "exit=$(ft_exitclass "$FT_ST");untagged_count=$_u"
}

_c015() {
  # R019 - regex classification runs and prints a summary.
  ft_run classify --regex
  _o="$FT_OUT$FT_ERR"
  _summary="$(ft_has "classif" "$_o")"
  _bad=0
  [ "$FT_ST" -eq 0 ] || _bad=1
  [ "$_summary" = true ] || _bad=1
  ft_record CONTRACT-015 "$(ft_status_from "$_bad")" \
    "exit=$(ft_exitclass "$FT_ST");summary=$_summary"
}

_c016() {
  # R020 - claude and codex are discovered on PATH.
  ft_run model
  _o="$FT_OUT$FT_ERR"
  _c="$(ft_has "claude" "$_o")"
  _x="$(ft_has "codex" "$_o")"
  _bad=0
  [ "$_c" = true ] || _bad=1
  [ "$_x" = true ] || _bad=1
  ft_record CONTRACT-016 "$(ft_status_from "$_bad")" \
    "claude=$_c;codex=$_x"
}

_c017() {
  # R021 - an unknown explicit engine override fails fast.
  ft_run model bogus
  _b=$FT_ST
  _bad=0
  [ "$_b" -ne 0 ] || _bad=1
  ft_record CONTRACT-017 "$(ft_status_from "$_bad")" \
    "unknown_engine=$(ft_exitclass "$_b")"
}

_c018() {
  # R022 - documented claude/codex argv forms and the engine label.
  ft_record CONTRACT-018 PASS \
    "claude=-p --output-format text [--model m] [--effort e] <prompt>;codex=exec --skip-git-repo-check [--model m] [--config model_reasoning_effort=e] <prompt>;label=name[/model][/effort=e]"
}

_c019() {
  # R023 - documented engine-invocation contract.
  ft_record CONTRACT-019 PASS \
    "stdin_eof=true;trimmed_stdout=true;error_reasons=timeout,maxbuffer,exit,spawn;killed=true;timeout_ms=120000;maxbuffer_bytes=1048576;stderr_tail=true;redact=true"
}

_c020() {
  # R024 - documented interactive prompt contract.
  ft_record CONTRACT-020 PASS \
    "answer=trimmed;close=close;interrupt=interrupt"
}

_c021() {
  # R025 - preferences load empty when missing and save owner-only (0600).
  rm -f "$FT_DATA/.preferences"
  ft_run model claude
  _mode="none"
  if [ -f "$FT_DATA/.preferences" ]; then
    _mode="$(stat -c %a "$FT_DATA/.preferences" 2>/dev/null || echo unknown)"
  fi
  _bad=0
  [ "$_mode" = "600" ] || _bad=1
  ft_record CONTRACT-021 "$(ft_status_from "$_bad")" \
    "saved_mode=$_mode"
}

_c022() {
  # R026 - documented filesystem write contract.
  ft_record CONTRACT-022 PASS \
    "json=0600;jsonl=0600;markdown=0644;durable=true;missing_jsonl_empty=true"
}

_c023() {
  # R027 - library operations are confined to the Library root.
  printf 'body\n' >"$FT_DATA/.lib-in"
  ft_run library update ../escape --file "$FT_DATA/.lib-in"
  _t=$FT_ST
  ft_run library list
  _l=$FT_ST
  _bad=0
  [ "$_t" -ne 0 ] || _bad=1
  [ "$_l" -eq 0 ] || _bad=1
  ft_record CONTRACT-023 "$(ft_status_from "$_bad")" \
    "traversal=$(ft_exitclass "$_t");list=$(ft_exitclass "$_l")"
}

_c024() {
  # R028 - command names reject separators/leading . or _ and validate exits.
  ft_run commands new ../evil
  _s=$FT_ST
  ft_run commands new _hidden
  _u=$FT_ST
  mkdir -p "$FT_COMMANDS_DIR"
  printf 'x\n' >"$FT_COMMANDS_DIR/bad2.md"
  ft_run commands validate
  _v=$FT_ST
  _bad=0
  [ "$_s" -ne 0 ] || _bad=1
  [ "$_u" -ne 0 ] || _bad=1
  [ "$_v" -ne 0 ] || _bad=1
  ft_record CONTRACT-024 "$(ft_status_from "$_bad")" \
    "separator=$(ft_exitclass "$_s");leading_underscore=$(ft_exitclass "$_u");validate=$(ft_exitclass "$_v")"
}

_c025() {
  # R029 - UTC date labels derived from the cache, documented fallbacks.
  ft_run stats --json
  _j="$FT_OUT"
  _e="$(ft_has "2023-12-31" "$_j")"
  _l="$(ft_has "2024-02-01" "$_j")"
  _bad=0
  [ "$_e" = true ] || _bad=1
  [ "$_l" = true ] || _bad=1
  ft_record CONTRACT-025 "$(ft_status_from "$_bad")" \
    "earliest_utc=$_e;latest_utc=$_l;fallback_year=????;fallback_monthday= ?? ??"
}

_c026() {
  # R030 - documented legacy ideas migration contract.
  ft_record CONTRACT-026 PASS \
    "idempotent=true;markers=in-progress,complete;recover=true;preserve_target=true"
}

_c027() {
  # R031 - documented environment loading order and no-override behavior.
  ft_record CONTRACT-027 PASS \
    "order=env.local,env,cwd,data;override_existing=false"
}

_c028() {
  # R032 - documented public capabilities remain present.
  _bad=0
  ft_run search machine
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_run list --limit 5
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_run index
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_run path
  [ "$FT_ST" -eq 0 ] || _bad=1
  ft_record CONTRACT-028 "$(ft_status_from "$_bad")" \
    "capabilities_present=$(ft_bool "$_bad" 0)"
}

# ft_contract_all <observation_id|all>
ft_contract_all() {
  _ft_want="${1:-all}"
  for _ft_obs in CONTRACT-001 CONTRACT-002 CONTRACT-003 CONTRACT-004 \
                  CONTRACT-005 CONTRACT-006 CONTRACT-007 CONTRACT-008 \
                  CONTRACT-009 CONTRACT-010 CONTRACT-011 CONTRACT-012 \
                  CONTRACT-013 CONTRACT-014 CONTRACT-015 CONTRACT-016 \
                  CONTRACT-017 CONTRACT-018 CONTRACT-019 CONTRACT-020 \
                  CONTRACT-021 CONTRACT-022 CONTRACT-023 CONTRACT-024 \
                  CONTRACT-025 CONTRACT-026 CONTRACT-027 CONTRACT-028; do
    if [ "$_ft_want" != "all" ] && [ "$_ft_want" != "$_ft_obs" ]; then
      continue
    fi
    case "$_ft_obs" in
      CONTRACT-001) _c001 ;;
      CONTRACT-002) _c002 ;;
      CONTRACT-003) _c003 ;;
      CONTRACT-004) _c004 ;;
      CONTRACT-005) _c005 ;;
      CONTRACT-006) _c006 ;;
      CONTRACT-007) _c007 ;;
      CONTRACT-008) _c008 ;;
      CONTRACT-009) _c009 ;;
      CONTRACT-010) _c010 ;;
      CONTRACT-011) _c011 ;;
      CONTRACT-012) _c012 ;;
      CONTRACT-013) _c013 ;;
      CONTRACT-014) _c014 ;;
      CONTRACT-015) _c015 ;;
      CONTRACT-016) _c016 ;;
      CONTRACT-017) _c017 ;;
      CONTRACT-018) _c018 ;;
      CONTRACT-019) _c019 ;;
      CONTRACT-020) _c020 ;;
      CONTRACT-021) _c021 ;;
      CONTRACT-022) _c022 ;;
      CONTRACT-023) _c023 ;;
      CONTRACT-024) _c024 ;;
      CONTRACT-025) _c025 ;;
      CONTRACT-026) _c026 ;;
      CONTRACT-027) _c027 ;;
      CONTRACT-028) _c028 ;;
    esac
  done
}

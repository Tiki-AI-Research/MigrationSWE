#!/bin/sh
# Shared helpers for the BATCH-F2P-0002 target-only postcondition probes.
#
# The same predicate is used on the source and target sides: FT_REPO points at
# the repository under test and each probe reports the raw observed status.
# Every potentially long-running command is wrapped in an explicit `timeout`;
# no network, root, or blocking command is ever invoked.
#
# The probe file lives under either source/batches/... or target/batches/...;
# the side is detected from $0 (or from an explicit FT_SIDE) so the correct
# repository is selected even when the integration runner does not export
# FT_REPO.

set -eu

FT_LIB_DIR="$(cd "$(dirname "$0")" && pwd)"
FT_ASSET_ROOT="$(cd "$FT_LIB_DIR/../../.." && pwd)"
FT_FIXTURES="$FT_ASSET_ROOT/fixtures/batches/BATCH-F2P-0002"
FT_BATCH_DIR="$FT_ASSET_ROOT/target/batches/BATCH-F2P-0002"

FT_SIDE="${FT_SIDE:-}"
if [ -z "$FT_SIDE" ]; then
  case "$0" in
    */source/batches/*) FT_SIDE=source ;;
    */target/batches/*) FT_SIDE=target ;;
    *) FT_SIDE=target ;;
  esac
fi

FT_TIMEOUT_BIN="$(command -v timeout || true)"
FT_GO_BIN="$(command -v go || true)"
FT_PY_BIN="$(command -v python3 || true)"

# resolve_repo selects the repository under test.
#   - FT_REPO wins when it is set explicitly.
#   - The source side must run against the TypeScript origin and never against
#     a Go repository, so that the target-only postcondition stays FAIL.
#   - The target side prefers the migrated working tree at /workspace/repo and
#     only falls back to the read-only authoring reference at /input/target
#     when /workspace/repo is not itself a repository.
resolve_repo() {
  if [ "$FT_SIDE" = source ]; then
    # A source repo is a TypeScript origin: it must not carry a Go entry point.
    if [ -n "${FT_REPO:-}" ] && [ -d "$FT_REPO" ] && [ ! -d "$FT_REPO/cmd/ft" ]; then
      printf '%s\n' "$FT_REPO"
      return 0
    fi
    if [ -d /input/source ]; then
      printf '%s\n' /input/source
    elif [ -f /workspace/repo/src/cli.ts ] && [ ! -d /workspace/repo/cmd/ft ]; then
      printf '%s\n' /workspace/repo
    else
      printf '%s\n' /input/source
    fi
  else
    # A target repo is a real repository, migrated or not.
    if [ -n "${FT_REPO:-}" ] && { [ -f "$FT_REPO/go.mod" ] || [ -f "$FT_REPO/package.json" ]; }; then
      printf '%s\n' "$FT_REPO"
      return 0
    fi
    if [ -f /workspace/repo/go.mod ]; then
      printf '%s\n' /workspace/repo
    elif [ -f /workspace/repo/package.json ]; then
      printf '%s\n' /workspace/repo
    elif [ -f /input/target/go.mod ]; then
      printf '%s\n' /input/target
    else
      printf '%s\n' /workspace/repo
    fi
  fi
}

FT_REPO_ROOT="$(resolve_repo)"

FT_WORK=""
FT_BIN=""

run_timed() {
  _ft_secs="$1"
  shift
  # Fail closed: never execute a potentially blocking command without a bound.
  # The harness must not be able to wait indefinitely, so a missing `timeout`
  # is an explicit error rather than an unbounded fallback.
  if [ -z "$FT_TIMEOUT_BIN" ]; then
    echo "timeout binary unavailable; refusing to run unbounded" >&2
    return 127
  fi
  "$FT_TIMEOUT_BIN" -k 5 "$_ft_secs" "$@"
}

FT_WORK="$(mktemp -d "${TMPDIR:-/tmp}/ftf2p.XXXXXX")"
trap 'rm -rf "$FT_WORK" 2>/dev/null || true' EXIT INT TERM

workdir() {
  printf '%s\n' "$FT_WORK"
}

if [ -n "$FT_GO_BIN" ] && [ -f "$FT_REPO_ROOT/go.mod" ]; then
  FT_BIN="$FT_WORK/ft"
  if ! ( cd "$FT_REPO_ROOT" && run_timed 600 python3 "$FT_ASSET_ROOT/shared/integration/build_cli.py" "$FT_REPO_ROOT" "$FT_BIN" ) >"$FT_WORK/build.log" 2>&1; then
    FT_BIN=""
  fi
fi

new_datadir() {
  _d="$(workdir)/data.$1"
  rm -rf "$_d"
  mkdir -p "$_d"
  printf '%s\n' "$_d"
}

have_python() { [ -n "$FT_PY_BIN" ]; }

emit() {
  printf '%s' "$3" | python3 "$FT_ASSET_ROOT/shared/integration/diagnostics.py" "$1" "$2"
}

report() {
  _obs="$1"
  shift
  set +e
  _ev="$("$@" 2>&1)"
  _rc=$?
  set -e
  case "$_rc" in
    0) emit "$_obs" PASS "$_ev" ;;
    2) emit "$_obs" INCONCLUSIVE "$_ev" ;;
    124|137) emit "$_obs" TIMEOUT "$_ev" ;;
    126|127) emit "$_obs" INFRA_ERROR "$_ev" ;;
    *) emit "$_obs" FAIL "$_ev" ;;
  esac
  exit 0
}

run_all() {
  _combined=0
  for _fn in "$@"; do
    set +e
    _out="$("$_fn" 2>&1)"
    _rc=$?
    set -e
    if [ "$_rc" -ne 0 ]; then
      printf '%s [%s]: %s\n' "$_fn" "$_rc" "$_out"
      # A demonstrated contract failure takes precedence over an adapter
      # limitation. Keep unsupported adaptation distinct from candidate FAIL.
      case "$_rc" in
        2) [ "$_combined" -ne 0 ] || _combined=2 ;;
        124|137) [ "$_combined" -eq 1 ] || _combined=124 ;;
        126|127) [ "$_combined" -eq 1 ] || [ "$_combined" -eq 124 ] || _combined=127 ;;
        *) _combined=1 ;;
      esac
    fi
  done
  [ "$_combined" -eq 0 ] || return "$_combined"
  printf 'ok: %s\n' "$*"
}

ensure_ft() {
  if [ -n "$FT_BIN" ] && [ -x "$FT_BIN" ]; then
    return 0
  fi
  [ -d "$FT_REPO_ROOT" ] || { echo "repo root missing: $FT_REPO_ROOT"; return 1; }
  [ -f "$FT_REPO_ROOT/go.mod" ] || { echo "no Go module"; return 1; }
  [ -n "$FT_GO_BIN" ] || { echo "go toolchain not available"; return 1; }
  _w="$(workdir)"
  FT_BIN="$_w/ft"
  if ! ( cd "$FT_REPO_ROOT" && run_timed 600 python3 "$FT_ASSET_ROOT/shared/integration/build_cli.py" "$FT_REPO_ROOT" "$FT_BIN" ) >"$_w/build.log" 2>&1; then
    echo "go build failed: $(tail -c 300 "$_w/build.log" 2>/dev/null)"
    FT_BIN=""
    return 1
  fi
  [ -x "$FT_BIN" ]
}

ft_run() {
  _d="$1"
  shift
  FT_DATA_DIR="$_d" run_timed 60 "$FT_BIN" "$@"
}

db_query() {
  _db="$1"
  _sql="$2"
  "$FT_PY_BIN" - "$_db" "$_sql" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    for row in con.execute(sys.argv[2]):
        print("\t".join("" if v is None else str(v) for v in row))
finally:
    con.close()
PY
}

# base_command_names: the documented command names are registered on the root
# command and the binary resolves the active data directory in-process.
base_command_names() {
  ensure_ft || return 1
  _h="$(run_timed 30 "$FT_BIN" --help 2>&1 || true)"
  _miss=""
  for _c in search list show stats classify classify-domains categories domains folders model index path paths status sample library commands; do
    printf '%s' "$_h" | grep -q -- "$_c" || _miss="$_miss $_c"
  done
  [ -z "$_miss" ] || { echo "missing commands:$_miss"; return 1; }
  _d="$(new_datadir names)"
  FT_DATA_DIR="$_d" run_timed 30 "$FT_BIN" path >/dev/null 2>&1 || { echo "ft path failed"; return 1; }
  echo "documented command names present"
}

# base_help_subcommands: the documented subcommands and their options parse and
# run (`list --limit` plus index).  Per-subcommand `--help` is not part of the
# documented contract, so it is not required here; command presence is covered
# by base_command_names.
base_help_subcommands() {
  ensure_ft || return 1
  _d="$(new_datadir subhelp)"
  cp "$FT_FIXTURES/bookmarks_fts_order.jsonl" "$_d/bookmarks.jsonl"
  ft_run "$_d" index >/dev/null 2>&1 || { echo "ft index failed"; return 1; }
  ft_run "$_d" list --limit 5 >/dev/null 2>&1 || { echo "ft list --limit 5 failed"; return 1; }
  echo "subcommand list options parse"
}

# base_both_names: `ft` and `fieldtheory` expose the same command tree and
# resolve the active data directory identically.
base_both_names() {
  ensure_ft || return 1
  _d="$(new_datadir bothnames)"
  _bin2="$(workdir)/fieldtheory"
  cp "$FT_BIN" "$_bin2"
  _h1="$(FT_DATA_DIR="$_d" run_timed 30 "$FT_BIN" --help 2>&1 || true)"
  _h2="$(FT_DATA_DIR="$_d" run_timed 30 "$_bin2" --help 2>&1 || true)"
  [ -n "$_h1" ] && [ -n "$_h2" ] || { echo "help output empty"; return 1; }
  for _c in search list show index path library commands; do
    printf '%s' "$_h1" | grep -q -- "$_c" || { echo "ft missing $_c"; return 1; }
    printf '%s' "$_h2" | grep -q -- "$_c" || { echo "fieldtheory missing $_c"; return 1; }
  done
  _p1="$(FT_DATA_DIR="$_d" run_timed 30 "$FT_BIN" path 2>&1)" || { echo "ft path failed"; return 1; }
  _p2="$(FT_DATA_DIR="$_d" run_timed 30 "$_bin2" path 2>&1)" || { echo "fieldtheory path failed"; return 1; }
  [ "$_p1" = "$_p2" ] || { echo "ft/fieldtheory disagree: $_p1 vs $_p2"; return 1; }
  echo "ft and fieldtheory accept the same command tree"
}

base_command_surface() {
  base_command_names
}

base_schema() {
  ensure_ft || return 1
  have_python || { echo "python3 unavailable"; return 2; }
  _d="$(new_datadir schema)"
  cp "$FT_FIXTURES/bookmarks_schema.jsonl" "$_d/bookmarks.jsonl"
  ft_run "$_d" index >/dev/null 2>&1 || { echo "ft index failed"; return 1; }
  "$FT_PY_BIN" - "$_d/bookmarks.db" "$FT_ASSET_ROOT/shared/integration" <<'PY'
import sqlite3, sys
sys.path.insert(0, sys.argv[2])
from fts_contract import validate_fts
exp = "id,tweet_id,url,text,author_handle,author_name,author_profile_image_url,posted_at,bookmarked_at,synced_at,conversation_id,in_reply_to_status_id,quoted_status_id,language,like_count,repost_count,reply_count,quote_count,bookmark_count,view_count,media_count,link_count,links_json,tags_json,ingested_via,categories,primary_category,github_urls,domains,primary_domain,quoted_tweet_json,article_title,article_text,article_site,enriched_at,folder_ids,folder_names".split(",")
con = sqlite3.connect(sys.argv[1])
cols = [r[1].casefold() for r in con.execute("PRAGMA table_info(bookmarks)")]
assert len(cols) == len(exp) and set(cols) == set(exp), ("columns", len(cols), cols)
indexed = set()
for row in con.execute("PRAGMA index_list(bookmarks)"):
    if row[4]:
        continue  # A partial index cannot satisfy the full-column contract.
    name = row[1].replace('"', '""')
    indexed.add(tuple(entry[2].casefold() if isinstance(entry[2], str) else None
                      for entry in con.execute('PRAGMA index_info("' + name + '")')))
for column in ['author_handle', 'posted_at', 'language', 'primary_category', 'primary_domain']:
    assert any(columns and columns[0] == column for columns in indexed), ("missing bookmark index", column, indexed)
sql = (con.execute("SELECT sql FROM sqlite_master WHERE name='bookmarks_fts' COLLATE NOCASE").fetchone() or [""])[0]
validate_fts(sql)
ver = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
assert str(ver) == "6", ver
print("37 columns, content-linked fts5, schema_version=6")
PY
}

base_bm25() {
  ensure_ft || return 1
  have_python || return 2
  run_timed 180 "$FT_PY_BIN" "$FT_ASSET_ROOT/shared/integration/cli_contract.py" bm25 "$FT_BIN"
}

base_show() {
  ensure_ft || return 1
  _d="$(new_datadir show)"
  cp "$FT_FIXTURES/bookmarks_fts_order.jsonl" "$_d/bookmarks.jsonl"
  ft_run "$_d" index >/dev/null 2>&1 || return 1
  _ok="$(ft_run "$_d" show 1 --json 2>/dev/null)" || { echo "ft show <known> failed"; return 1; }
  printf '%s' "$_ok" | grep -q '"id"' || { echo "show output missing id"; return 1; }
  ft_run "$_d" show does-not-exist --json >/dev/null 2>&1
  _rc=$?
  [ "$_rc" -ne 0 ] || { echo "unknown id exited 0"; return 1; }
  echo "show known ok, unknown exits $_rc"
}

base_list() {
  ensure_ft || return 1
  _d="$(new_datadir list)"
  cp "$FT_FIXTURES/bookmarks_fts_order.jsonl" "$_d/bookmarks.jsonl"
  ft_run "$_d" index >/dev/null 2>&1 || return 1
  ft_run "$_d" list --query 'machine' --limit 5 --offset 0 --json >/dev/null 2>&1 || { echo "ft list failed"; return 1; }
  echo "ft list with filters ok"
}

base_domains() {
  ensure_ft || return 1
  _d="$(new_datadir domains)"
  cp "$FT_FIXTURES/bookmarks_schema.jsonl" "$_d/bookmarks.jsonl"
  ft_run "$_d" index >/dev/null 2>&1 || return 1
  ft_run "$_d" domains >/dev/null 2>&1 || { echo "ft domains failed"; return 1; }
  echo "ft domains ok"
}

base_stats() {
  ensure_ft || return 1
  _d="$(new_datadir stats)"
  cp "$FT_FIXTURES/bookmarks_schema.jsonl" "$_d/bookmarks.jsonl"
  ft_run "$_d" index >/dev/null 2>&1 || return 1
  ft_run "$_d" stats --json >/dev/null 2>&1 || { echo "ft stats failed"; return 1; }
  echo "ft stats ok"
}

base_classify() {
  ensure_ft || return 1
  have_python || return 2
  _d="$(new_datadir classify)"
  cp "$FT_FIXTURES/classify_regex.jsonl" "$_d/bookmarks.jsonl"
  ft_run "$_d" index >/dev/null 2>&1 || return 1
  ft_run "$_d" classify --regex >/dev/null 2>&1 || { echo "ft classify --regex failed"; return 1; }
  _p1="$(db_query "$_d/bookmarks.db" "SELECT primary_category FROM bookmarks WHERE id='1'")"
  _p2="$(db_query "$_d/bookmarks.db" "SELECT primary_category FROM bookmarks WHERE id='2'")"
  [ "$_p1" = "security" ] || { echo "id=1 primary=$_p1 want security"; return 1; }
  [ "$_p2" = "commerce" ] || { echo "id=2 primary=$_p2 want commerce"; return 1; }
  echo "classify: id1=$_p1 id2=$_p2"
}

base_exitcodes() {
  ensure_ft || return 1
  _d="$(new_datadir exits)"
  cp "$FT_FIXTURES/bookmarks_schema.jsonl" "$_d/bookmarks.jsonl"
  ft_run "$_d" index >/dev/null 2>&1 || return 1
  _cdir="$(workdir)/cmdroot"
  mkdir -p "$_cdir"
  cp "$FT_FIXTURES/commands_invalid.md" "$_cdir/bad.md"
  FT_COMMANDS_DIR="$_cdir" FT_DATA_DIR="$_d" run_timed 30 "$FT_BIN" commands validate bad >/dev/null 2>&1
  _rc1=$?
  [ "$_rc1" -ne 0 ] || { echo "commands validate invalid exited 0"; return 1; }
  ft_run "$_d" search >/dev/null 2>&1
  _rc2=$?
  [ "$_rc2" -ne 0 ] || { echo "search without query exited 0"; return 1; }
  echo "failure exit codes: validate=$_rc1 parse=$_rc2"
}

base_pathguard() {
  ensure_ft || return 1
  _d="$(new_datadir pathguard)"
  _lib="$(workdir)/libroot"
  rm -rf "$_lib"
  mkdir -p "$_lib"
  FT_DATA_DIR="$_d" FT_LIBRARY_DIR="$_lib" run_timed 30 "$FT_BIN" commands new ../evil >/dev/null 2>&1
  _rc1=$?
  FT_DATA_DIR="$_d" FT_LIBRARY_DIR="$_lib" run_timed 30 "$FT_BIN" library show ../../etc/passwd >/dev/null 2>&1
  _rc2=$?
  FT_DATA_DIR="$_d" FT_LIBRARY_DIR="$_lib" run_timed 30 "$FT_BIN" commands new _hidden >/dev/null 2>&1
  _rc3=$?
  [ "$_rc1" -ne 0 ] && [ "$_rc2" -ne 0 ] && [ "$_rc3" -ne 0 ] || { echo "expected non-zero: $_rc1 $_rc2 $_rc3"; return 1; }
  if [ -e "$(dirname "$_lib")/evil.md" ] || [ -e "$(dirname "$_lib")/evil" ]; then
    echo "traversal escaped the root"
    return 1
  fi
  echo "traversal rejected rc=$_rc1/$_rc2/$_rc3"
}

base_paths() {
  ensure_ft || return 1
  have_python || return 2
  _envdir="$(workdir)/envdata"
  mkdir -p "$_envdir"
  _out="$(FT_DATA_DIR="$_envdir" run_timed 30 "$FT_BIN" paths --json 2>/dev/null)" || { echo "ft paths --json failed"; return 1; }
  "$FT_PY_BIN" - "$_out" "$_envdir" <<'PY'
import json, sys
doc = json.loads(sys.argv[1])
assert isinstance(doc, dict), type(doc)
for key in ("active", "canonical", "legacy"):
    assert key in doc, key
assert doc["active"]["bookmarksDir"] == sys.argv[2], doc["active"]
assert doc["active"]["libraryDir"] == sys.argv[2] + "/md", doc["active"]
assert doc["active"]["commandsDir"] == sys.argv[2] + "/md/Commands", doc["active"]
print("paths json active/canonical/legacy present")
PY
}

base_selfheal() {
  ensure_ft || return 1
  have_python || return 2
  _d="$(new_datadir heal)"
  cp "$FT_FIXTURES/bookmarks_schema.jsonl" "$_d/bookmarks.jsonl"
  ft_run "$_d" index >/dev/null 2>&1 || { echo "initial index failed"; return 1; }
  "$FT_PY_BIN" - "$_d/bookmarks.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("UPDATE bookmarks SET primary_category='tool' WHERE id='1'")
# Remove dependent maintenance triggers while constructing the older schema.
# Trigger-based FTS maintenance is a valid candidate implementation.
for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='bookmarks'").fetchall():
    con.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
con.execute("ALTER TABLE bookmarks DROP COLUMN domains")
con.execute("ALTER TABLE bookmarks DROP COLUMN article_text")
con.commit()
con.close()
PY
  ft_run "$_d" index >/dev/null 2>&1 || { echo "refresh failed"; return 1; }
  _kept="$(db_query "$_d/bookmarks.db" "SELECT primary_category FROM bookmarks WHERE id='1'")"
  [ "$_kept" = "tool" ] || { echo "classification not preserved: $_kept"; return 1; }
  _cols="$(db_query "$_d/bookmarks.db" "SELECT group_concat(name) FROM pragma_table_info('bookmarks')")"
  printf '%s' "$_cols" | grep -q 'article_text' || { echo "article_text not healed"; return 1; }
  ft_run "$_d" index --force >/dev/null 2>&1 || { echo "force index failed"; return 1; }
  _reset="$(db_query "$_d/bookmarks.db" "SELECT primary_category FROM bookmarks WHERE id='1'")"
  [ "$_reset" = "unclassified" ] || { echo "force did not reset: $_reset"; return 1; }
  echo "self-heal kept=$_kept force-reset=$_reset"
}

base_no_node() {
  ensure_ft || return 1
  _d="$(new_datadir nonode)"
  cp "$FT_FIXTURES/bookmarks_schema.jsonl" "$_d/bookmarks.jsonl"
  PATH=/nonexistent FT_DATA_DIR="$_d" run_timed 30 "$FT_BIN" path >/dev/null 2>&1 || { echo "path failed without node"; return 1; }
  PATH=/nonexistent FT_DATA_DIR="$_d" run_timed 60 "$FT_BIN" index >/dev/null 2>&1 || { echo "index failed without node"; return 1; }
  PATH=/nonexistent FT_DATA_DIR="$_d" run_timed 60 "$FT_BIN" classify --regex >/dev/null 2>&1 || { echo "classify failed without node"; return 1; }
  echo "in-process with empty PATH"
}

base_native() {
  ensure_ft || return 1
  _d="$(new_datadir native)"
  cp "$FT_FIXTURES/bookmarks_schema.jsonl" "$_d/bookmarks.jsonl"
  PATH=/nonexistent FT_DATA_DIR="$_d" run_timed 30 "$FT_BIN" path >/dev/null 2>&1 || { echo "path failed without node"; return 1; }
  PATH=/nonexistent FT_DATA_DIR="$_d" run_timed 60 "$FT_BIN" index >/dev/null 2>&1 || { echo "index failed without node"; return 1; }
  echo "native Go binary runs without Node"
}

base_entry() {
  ensure_ft || return 1
  _d="$(new_datadir entry)"
  _bin2="$(workdir)/fieldtheory"
  cp "$FT_BIN" "$_bin2"
  _out="$(PATH=/nonexistent FT_DATA_DIR="$_d" run_timed 30 "$_bin2" path 2>&1)" || { echo "fieldtheory path failed"; return 1; }
  [ "$_out" = "$_d" ] || { echo "expected $_d got $_out"; return 1; }
  echo "fieldtheory path in-process: $_out"
}

# node_public_wiring: the repository's public Node test command (`npm test`)
# must still run the public test files through tsx (`node:test`).  The exact
# command spelling is not part of the contract -- the instruction allows adding
# test wiring and only forbids changing the behavioural assertions of the
# existing public tests -- so an equivalent assembly (including a wrapper
# script that invokes tsx) is accepted.  Removing the tsx public-suite wiring
# is not.
node_public_wiring() {
  python3 - "$1" <<'PY'
import json
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])


def runs_public_suite(command: str) -> bool:
    lowered = command.casefold()
    if "tsx" not in lowered:
        return False
    # The public test files must still be the runner's target.
    return bool(re.search(r"\btests\b|\.test\.[a-z]+|\.spec\.[a-z]+", lowered))


try:
    manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    sys.exit(1)
scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
command = scripts.get("test") if isinstance(scripts, dict) else None
if not isinstance(command, str) or not command.strip():
    sys.exit(1)
if runs_public_suite(command):
    sys.exit(0)
# Follow one local wrapper script the test command delegates to.
for match in re.finditer(
    r"(?:^|\s)(?:sh|bash|node|tsx|npx|pnpm|yarn)\s+([^\s;&|()]+\.(?:sh|mjs|cjs|js|mts|ts))",
    command,
):
    candidate = root / match.group(1)
    if not candidate.is_file():
        continue
    try:
        body = candidate.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    if runs_public_suite(body):
        sys.exit(0)
sys.exit(1)
PY
}

base_test_suite() {
  [ -d "$FT_REPO_ROOT" ] || { echo "repo root missing"; return 1; }
  [ -n "$FT_GO_BIN" ] || { echo "go toolchain not available"; return 2; }
  [ -f "$FT_REPO_ROOT/go.mod" ] || { echo "no go.mod"; return 1; }
  if ! ( cd "$FT_REPO_ROOT" && run_timed 900 "$FT_GO_BIN" test ./... ) >"$(workdir)/gotest.log" 2>&1; then
    echo "optional candidate Go tests failed (diagnostic): $(tail -c 300 "$(workdir)/gotest.log")"
  fi
  [ -d "$FT_REPO_ROOT/tests" ] || { echo "public tests directory missing"; return 1; }
  [ -f "$FT_REPO_ROOT/package.json" ] || { echo "package.json missing"; return 1; }
  node_public_wiring "$FT_REPO_ROOT" || { echo "public node test wiring changed"; return 1; }
  echo "candidate Go tests executed; public Node test wiring intact"
}

# base_module_deps: the target is a native Go module that must not delegate to
# the TypeScript/Node runtime.  Specific third-party libraries are an
# implementation choice, not part of the documented contract, so they are not
# required here.
base_module_deps() {
  [ -f "$FT_REPO_ROOT/go.mod" ] || { echo "no go.mod"; return 1; }
  grep -Eq '^[[:space:]]*module[[:space:]]+[^[:space:]]+' "$FT_REPO_ROOT/go.mod" || { echo "module declaration missing"; return 1; }
  # A module path or comment containing "node"/"tsx" says nothing about
  # runtime delegation. Build/run probes and the runtime detector own that
  # requirement; do not reject valid Go identifiers here.
  echo "go.mod declares a Go module"
}

base_buildrun() {
  ensure_ft || return 1
  _d="$(new_datadir buildrun)"
  _bin2="$(workdir)/fieldtheory"
  cp "$FT_BIN" "$_bin2"
  PATH=/nonexistent FT_DATA_DIR="$_d" run_timed 30 "$FT_BIN" path >/dev/null 2>&1 || { echo "ft run failed"; return 1; }
  PATH=/nonexistent FT_DATA_DIR="$_d" run_timed 30 "$_bin2" --help >/dev/null 2>&1 || { echo "fieldtheory run failed"; return 1; }
  echo "go build + ft/fieldtheory entrypoint ok"
}

base_integrity() {
  ensure_ft || return 1
  _d="$(new_datadir integrity)"
  cp "$FT_FIXTURES/classify_regex.jsonl" "$_d/bookmarks.jsonl"
  ft_run "$_d" index >/dev/null 2>&1 || { echo "index failed"; return 1; }
  _hit="$(ft_run "$_d" search 'remote code execution' --json 2>/dev/null || true)"
  printf '%s' "$_hit" | grep -q '"id"' || { echo "expected search hit"; return 1; }
  _miss="$(ft_run "$_d" search 'zzzznomatchterm' --json 2>/dev/null || true)"
  printf '%s' "$_miss" | grep -q '"id"' && { echo "mutated query returned rows"; return 1; }
  _cdir="$(workdir)/cmdroot2"
  mkdir -p "$_cdir"
  cp "$FT_FIXTURES/commands_valid.md" "$_cdir/good.md"
  cp "$FT_FIXTURES/commands_invalid.md" "$_cdir/bad.md"
  FT_COMMANDS_DIR="$_cdir" FT_DATA_DIR="$_d" run_timed 30 "$FT_BIN" commands validate good >/dev/null 2>&1 || { echo "valid command rejected"; return 1; }
  FT_COMMANDS_DIR="$_cdir" FT_DATA_DIR="$_d" run_timed 30 "$FT_BIN" commands validate bad >/dev/null 2>&1 && { echo "invalid command accepted"; return 1; }
  # Runtime assets are read-only. Create a writable fixture instead of copying
  # their mode, and stop if setup fails before judging the candidate validator.
  if ! { cat "$FT_FIXTURES/commands_valid.md" && printf '\nCredential example: sk-ABCDEFGHIJKLMNOPQRSTUVWX\n'; } >"$_cdir/credential.md"; then
    echo "could not prepare secret-like command fixture"
    return 1
  fi
  FT_COMMANDS_DIR="$_cdir" FT_DATA_DIR="$_d" run_timed 30 "$FT_BIN" commands validate credential >/dev/null 2>&1 && { echo "secret-like command value accepted"; return 1; }
  echo "real search/validation logic responds to inputs"
}

base_engine_gotest() {
  _pattern="$1"
  [ -d "$FT_REPO_ROOT" ] || { echo "repo root missing"; return 1; }
  [ -n "$FT_GO_BIN" ] || { echo "go toolchain not available"; return 2; }
  [ -f "$FT_REPO_ROOT/go.mod" ] || { echo "no go.mod"; return 1; }
  _repo="$(workdir)/repo.$_pattern"
  rm -rf "$_repo"
  mkdir -p "$_repo"
  if command -v tar >/dev/null 2>&1; then
    run_timed 120 sh -c '( cd "$1" && tar cf - --exclude=.git --exclude=.gomodcache --exclude=node_modules . ) | ( cd "$2" && tar xf - )' sh "$FT_REPO_ROOT" "$_repo" 2>/dev/null || { echo "could not copy repo"; return 2; }
  else
    run_timed 120 cp -a "$FT_REPO_ROOT/." "$_repo/" 2>/dev/null || { echo "could not copy repo"; return 2; }
    rm -rf "$_repo/.git" 2>/dev/null || true
  fi
  _pdir="$_repo/.f2pprobe"
  mkdir -p "$_pdir"
  cp "$FT_BATCH_DIR/gotest/f2p_probe_test.go" "$_pdir/f2p_probe_test.go"
  if ! ( cd "$_repo" && run_timed 300 "$FT_GO_BIN" test ./.f2pprobe/ -run "$_pattern" -count=1 ) >"$(workdir)/engine.log" 2>&1; then
    echo "go test $_pattern failed: $(tail -c 400 "$(workdir)/engine.log")"
    return 1
  fi
  echo "probe $_pattern passed"
}

base_viz() {
  ensure_ft || return 1
  _d="$(new_datadir viz)"
  cp "$FT_FIXTURES/bookmarks_schema.jsonl" "$_d/bookmarks.jsonl"
  ft_run "$_d" index >/dev/null 2>&1 || return 1
  ft_run "$_d" viz >"$(workdir)/viz.out" 2>&1 || { echo "documented viz command failed"; return 1; }
  [ -s "$(workdir)/viz.out" ] || { echo "viz output empty"; return 1; }
  ft_run "$_d" skill show >"$(workdir)/skill.out" 2>&1 || { echo "documented skill show command failed"; return 1; }
  [ -s "$(workdir)/skill.out" ] || { echo "skill show output empty"; return 1; }
}

base_engine_argv() { run_timed 210 python3 "$FT_ASSET_ROOT/shared/integration/module_contract.py" "$FT_REPO_ROOT" argv; }
base_engine_invoke() { run_timed 270 python3 "$FT_ASSET_ROOT/shared/integration/module_contract.py" "$FT_REPO_ROOT" bridge; }
base_prompt() { run_timed 210 python3 "$FT_ASSET_ROOT/shared/integration/module_contract.py" "$FT_REPO_ROOT" prompt; }

f2p_025() { run_all base_help_subcommands base_integrity; }
f2p_026() { run_all base_test_suite base_both_names; }
f2p_027() { run_all base_domains base_bm25; }
f2p_028() { run_all base_classify base_bm25; }
f2p_029() { run_all base_show base_bm25; }
f2p_030() { run_all base_both_names base_integrity; }
f2p_031() { run_all base_native base_command_names base_viz; }
f2p_032() { run_all base_schema base_show; }
f2p_033() { run_all base_test_suite base_command_names; }
f2p_034() { run_all base_pathguard; }
f2p_035() { run_all base_schema base_stats; }
base_alias_errors() {
  ensure_ft || return 1
  _d="$(new_datadir alias-errors)"
  if ft_run "$_d" bookmarks path --quality-unknown-flag >/dev/null 2>&1; then
    echo "bookmarks alias accepted an unknown flag"; return 1
  fi
}
f2p_036() { run_all base_schema base_exitcodes base_alias_errors; }
f2p_037() { run_all base_pathguard base_paths; }
f2p_038() { run_all base_test_suite base_buildrun; }
f2p_039() { run_all base_buildrun base_module_deps base_integrity; }
f2p_040() { run_all base_selfheal; }
f2p_041() { run_all base_engine_argv; }
f2p_042() { run_all base_entry base_no_node; }
f2p_043() { run_all base_prompt; }
f2p_044() { run_all base_classify; }
f2p_045() { run_all base_engine_invoke; }
f2p_046() { run_all base_no_node base_paths; }
f2p_047() { run_all base_selfheal base_engine_argv; }
f2p_048() { run_all base_bm25 base_list; }

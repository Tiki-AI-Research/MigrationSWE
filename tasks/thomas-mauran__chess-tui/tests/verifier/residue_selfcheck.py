#!/usr/bin/env python3
"""Regression self-check for the deceptive source-fallback detection.

The hard gate ``no_deceptive_source_fallback`` must be insensitive to how an
attacker spells the source-runtime executable *and* to where the payload lives
or how it is materialised.  The scan therefore covers, with equal strength:

* target-language constant folding: plain literals, ``+`` concatenation,
  slice/array literals read back through ``x[i]``, ``fmt.Sprintf``,
  ``strings.Join``/``strings.*`` helpers, ``[]byte`` + ``string(...)`` and
  inline expressions that never reach a named variable;
* encoded payloads: base64/hex literals decoded in the same file;
* embedded payloads: a ``//go:embed``-ed asset (``run.sh``) is source that
  reaches the process at run time, so it is scanned too;
* scan surface: a fallback under ``build/`` (or any non-source directory) is
  still scanned -- only VCS/dependency caches are pruned;
* resolved program names: ``exec.Command(fallbackBinary)`` reconstructs the
  canonical ``exec.Command("chess-tui`` call so a forwarded program name hits;
* source -> sink taint: an *opaque* payload (decoder/embed/read, propagated
  across statements and helpers) that names what a process/plugin sink executes
  is a violation even when the concrete token is not statically visible.

Run directly (``python3 tests/verifier/residue_selfcheck.py``); it exits
non-zero when a family member is no longer detected or when a legitimate
implementation is falsely flagged.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from residue import _dependency_contains, scan_residue_rules  # noqa: E402

# --- attacker family: a literal/assembled source invocation ----------------
_ATTACKS = {
    "concat-scalars": """package legacy

import "os/exec"

func legacyMove(fen string) ([]byte, error) {
	program := "car" + "go"
	payload := "./tar" + "get/release/" + "chess-tui"
	return exec.Command(program, "run", "--release", payload, fen).Output()
}
""",
    "slice-then-index": """package legacy

import "os/exec"

func legacyMove(fen string) ([]byte, error) {
	argv := []string{"car" + "go", "ru" + "n", "--release", "./tar" + "get/release/" + "chess-tui", fen}
	cmd := exec.Command(argv[0], argv[1:]...)
	return cmd.Output()
}
""",
    "fixed-array-index": """package legacy

import "os/exec"

func legacyMove(fen string) ([]byte, error) {
	argv := [5]string{"car" + "go", "ru" + "n", "--release", "./tar" + "get/release/" + "chess-tui", fen}
	return exec.Command(argv[0], argv[1], argv[2], argv[3], argv[4]).Output()
}
""",
    "sprintf-verbs": """package legacy

import (
	"fmt"
	"os/exec"
)

func legacyMove(fen string) ([]byte, error) {
	program := fmt.Sprintf("%s%s", "car", "go")
	payload := fmt.Sprintf("./%s/%s/chess-tui", "target", "release")
	return exec.Command(program, "run", "--release", payload, fen).Output()
}
""",
    "strings-join": """package legacy

import (
	"os/exec"
	"strings"
)

func legacyMove(fen string) ([]byte, error) {
	program := strings.Join([]string{"car", "go"}, "")
	payload := strings.Join([]string{"./target", "release", "chess-tui"}, "/")
	return exec.Command(program, "run", "--release", payload, fen).Output()
}
""",
    "byte-slice-string": """package legacy

import "os/exec"

func legacyMove(fen string) ([]byte, error) {
	program := string([]byte{'c', 'a', 'r', 'g', 'o'})
	payload := string([]byte("./target/release/chess-tui"))
	return exec.Command(program, "run", "--release", payload, fen).Output()
}
""",
    "inline-concat": """package legacy

import "os/exec"

func legacyMove(fen string) ([]byte, error) {
	return exec.Command("car"+"go", "run", "--release", "./target/release/chess-tui", fen).Output()
}
""",
    "inline-sprintf": """package legacy

import (
	"fmt"
	"os/exec"
)

func legacyMove(fen string) ([]byte, error) {
	return exec.Command(fmt.Sprintf("%s%s", "car", "go"), "run", "--release", "./target/release/chess-tui", fen).Output()
}
""",
    "case-helper": """package legacy

import (
	"os/exec"
	"strings"
)

func legacyMove(fen string) ([]byte, error) {
	program := strings.ToLower("CARGO")
	payload := strings.ReplaceAll("./TARGET/RELEASE/chess-tui", "TARGET/RELEASE", "target/release")
	return exec.Command(program, "--release", payload, fen).Output()
}
""",
    # --- encoded payload: base64 literal decoded in the same file -----------
    "base64-literal-payload": """package legacy

import (
	"encoding/base64"
	"os/exec"
)

const plan = "Y2FyZ28gcnVuIC0tcmVsZWFzZSAuL3RhcmdldC9yZWxlYXNlL2NoZXNzLXR1aQ=="

func legacyMove(fen string) ([]byte, error) {
	raw, _ := base64.StdEncoding.DecodeString(plan)
	return exec.Command("sh", "-c", string(raw), fen).Output()
}
""",
}

# --- attacker family: an opaque, run-time materialised payload -------------
_OPAQUE_ATTACKS = {
    "base64-decoded-argv": """package legacy

import (
	"encoding/base64"
	"os/exec"
	"strings"
)

const planBlob = "Y2FyZ28gcnVuIC0tcmVsZWFzZSAuL3RhcmdldC9yZWxlYXNlL2NoZXNzLXR1aQ=="

func legacyMove(fen string) ([]byte, error) {
	argv := append(decodePlan(planBlob), fen)
	return exec.Command(argv[0], argv[1:]...).Output()
}

func decodePlan(blob string) []string {
	raw, err := base64.StdEncoding.DecodeString(blob)
	if err != nil {
		return nil
	}
	return strings.Fields(string(raw))
}
""",
    "read-then-exec": """package legacy

import (
	"os"
	"os/exec"
	"strings"
)

func legacyMove(fen string) ([]byte, error) {
	blob, err := os.ReadFile("/tmp/plan.txt")
	if err != nil {
		return nil, err
	}
	argv := strings.Fields(string(blob))
	return exec.Command(argv[0], argv[1:]...).Output()
}
""",
    "embed-write-then-exec": """package legacy

import (
	_ "embed"
	"os"
	"os/exec"
	"path/filepath"
)

//go:embed run.sh
var legacyScript string

func legacyMove(fen string) ([]byte, error) {
	dir, err := os.MkdirTemp("", "legacy")
	if err != nil {
		return nil, err
	}
	path := filepath.Join(dir, "run.sh")
	if err := os.WriteFile(path, []byte(legacyScript), 0o755); err != nil {
		return nil, err
	}
	return exec.Command(path, fen).Output()
}
""",
}

# --- attacker family: a forwarded source program name ----------------------
_PROGRAM_ATTACKS = {
    "variable-program-name": """package legacy

import (
	"os"
	"os/exec"
)

const fallbackBinary = "chess-tui"

func SpawnFallback() error {
	return exec.Command(fallbackBinary, os.Args[1:]...).Run()
}
""",
    "slice-program-name": """package legacy

import "os/exec"

func SpawnFallback() error {
	argv := []string{"chess-tui", "play"}
	return exec.Command(argv[0], argv[1:]...).Run()
}
""",
}

# --- attacker family: a cross-file / aliased source fallback ----------------
# The payload may be decoded in one file and consumed by a sink in another, and
# the sink package may be renamed (``import ex "os/exec"``) or dot-imported
# (``import . "os/exec"``).  All of these reach the same source runtime.
_CROSSFILE_ATTACKS: dict[str, tuple[str, dict[str, str]]] = {
    "crossfile-decoded-subprocess": (
        """package legacy

import "os/exec"

func legacyMove(fen string) ([]byte, error) {
	argv := append(plan(planBlob), fen)
	return exec.Command(argv[0], argv[1:]...).Output()
}

const planBlob = "Y2FyZ28gcnVuIC0tcmVsZWFzZSAuL3RhcmdldC9yZWxlYXNlL2NoZXNzLXR1aQ=="
""",
        {
            "decode.go": """package legacy

import (
	"encoding/base64"
	"strings"
)

func plan(blob string) []string {
	raw, err := base64.StdEncoding.DecodeString(blob)
	if err != nil {
		return nil
	}
	return strings.Fields(string(raw))
}
"""
        },
    ),
    "alias-decoder-crossfile": (
        """package legacy

import ex "os/exec"

const blob = "Y2FyZ28gcnVuIC0tcmVsZWFzZSAuL3RhcmdldC9yZWxlYXNlL2NoZXNzLXR1aQ=="

func move(fen string) ([]byte, error) {
	argv := append(plan(blob), fen)
	return ex.Command(argv[0], argv[1:]...).Output()
}
""",
        {
            "decode.go": """package legacy

import (
	b64 "encoding/base64"
	"strings"
)

func plan(blob string) []string {
	raw, _ := b64.StdEncoding.DecodeString(blob)
	return strings.Fields(string(raw))
}
"""
        },
    ),
    "wrapper-param-forward": (
        """package legacy

import "os"

func Go() error {
	return spawn(os.Getenv("CHESS_TUI_ENGINE"))
}
""",
        {
            "spawn.go": """package legacy

import "os/exec"

func spawn(p string) error {
	return exec.Command(p).Run()
}
"""
        },
    ),
}

_ALIAS_ATTACKS: dict[str, str] = {
    "exec-pkg-alias": """package legacy

import (
	ex "os/exec"
	"os"
)

func FallbackEngine(fen string) error {
	program := os.Getenv("CHESS_TUI_ENGINE")
	argv := append([]string{program}, "uci")
	cmd := ex.Command(argv[0], argv[1:]...)
	return cmd.Run()
}
""",
    "plugin-alias": """package pluginload

import (
	"os"
	pl "plugin"
)

func LoadEnginePlugin() error {
	name := os.Getenv("CHESS_TUI_PLUGIN")
	_, err := pl.Open(name)
	return err
}
""",
    "literal-exec-cargo-alias": """package legacy

import ex "os/exec"

func RunLegacyCargo() error {
	cmd := ex.Command("cargo", "run", "--release")
	return cmd.Run()
}
""",
    "dotimport-exec": """package legacy

import (
	. "os/exec"
	"os"
)

func FallbackEngine() error {
	program := os.Getenv("CHESS_TUI_ENGINE")
	return Command(program).Run()
}
""",
}

# --- attacker family: constant-folding gaps (r5 adversarial RC-01) ----------
# The source-runtime name may be spelled as any constant expression.  The folder
# must resolve compound assignment, map-literal indexing, numeric/rune assembly
# and ``%c`` format verbs, not just ``+`` concatenation of literals.
_FOLD_ATTACKS: dict[str, str] = {
    "fold-plus-eq": """package legacy

import "os/exec"

func FallbackEngine() error {
	program := "car"
	program += "go"
	cmd := exec.Command(program, "run", "--release")
	return cmd.Run()
}
""",
    "fold-map-index": """package legacy

import "os/exec"

func FallbackEngine() error {
	programs := map[string]string{"engine": "cargo"}
	name := programs["engine"]
	cmd := exec.Command(name, "run", "--release")
	return cmd.Run()
}
""",
    "fold-rune-concat": """package legacy

import "os/exec"

func FallbackEngine() error {
	program := string(rune(99)) + string(rune(97)) + string(rune(114)) + string(rune(103)) + string(rune(111))
	cmd := exec.Command(program, "run", "--release")
	return cmd.Run()
}
""",
    "fold-sprintf-char": """package legacy

import (
	"fmt"
	"os/exec"
)

func FallbackEngine() error {
	program := fmt.Sprintf("%c%c%c%c%c", 99, 97, 114, 103, 111)
	cmd := exec.Command(program, "run", "--release")
	return cmd.Run()
}
""",
    # attacker self-test variants (not in the r5 reports)
    "fold-plus-eq-cmd-struct": """package legacy

import "os/exec"

func FallbackEngine() error {
	program := "car"
	program += "go"
	cmd := exec.Cmd{Path: program, Args: []string{program, "run", "--release"}}
	return cmd.Run()
}
""",
    "fold-map-index-sprintf": """package legacy

import (
	"fmt"
	"os/exec"
)

func FallbackEngine() error {
	table := map[string]string{"a": "cargo"}
	verb := fmt.Sprintf("%s", table["a"])
	cmd := exec.Command(verb, "run", "--release")
	return cmd.Run()
}
""",
}

# --- attacker family: multi-level wrapper forwarding ------------------------
# ``Entry`` hands the environment value to ``mid`` which forwards to ``spawn``
# which reaches the sink: the wrapper closure must be transitive.
_WRAPPER_CHAIN_ATTACKS: dict[str, tuple[str, dict[str, str]]] = {
    "wrapper-chain-2level": (
        """package legacy

import "os"

func Entry() error {
	return mid(os.Getenv("CHESS_TUI_ENGINE"))
}
""",
        {
            "mid.go": """package legacy

func mid(p string) error {
	return spawn(p)
}
""",
            "spawn.go": """package legacy

import "os/exec"

func spawn(p string) error {
	return exec.Command(p).Run()
}
""",
        },
    ),
    # attacker self-test variant: three levels of forwarding
    "wrapper-chain-3level": (
        """package legacy

import "os"

func Entry() error {
	return a(os.Getenv("CHESS_TUI_ENGINE"))
}
""",
        {
            "a.go": """package legacy

func a(p string) error {
	return b(p)
}
""",
            "b.go": """package legacy

func b(p string) error {
	return c(p)
}
""",
            "c.go": """package legacy

import "os/exec"

func c(p string) error {
	return exec.Command(p).Run()
}
""",
        },
    ),
}

# --- attacker family: opaque payload parked in a struct field --------------
_FIELD_ATTACKS: dict[str, str] = {
    "field-pass-env": """package legacy

import (
	"os"
	"os/exec"
)

type plan struct {
	Program string
	Args    []string
}

func Move(fen string) ([]byte, error) {
	p := plan{Program: os.Getenv("CHESS_TUI_ENGINE")}
	return exec.Command(p.Program, fen).Output()
}
""",
    # attacker self-test variants (not in the r5 reports)
    "field-pass-nested": """package legacy

import (
	"os"
	"os/exec"
)

type inner struct{ Program string }
type outer struct{ In inner }

func Move(fen string) ([]byte, error) {
	o := outer{In: inner{Program: os.Getenv("CHESS_TUI_ENGINE")}}
	return exec.Command(o.In.Program, fen).Output()
}
""",
    "field-pass-reassigned": """package legacy

import (
	"os"
	"os/exec"
)

type plan struct{ Program string }

func Move(fen string) ([]byte, error) {
	env := os.Getenv("CHESS_TUI_ENGINE")
	p := plan{}
	p.Program = env
	return exec.Command(p.Program, fen).Output()
}
""",
}


# --- attacker family: function-value (higher-order) indirection --------------
# ``runone(spawn, env)`` hands the *function* that reaches the sink as an
# argument value; the wrapper closure must resolve function-valued parameters
# (named functions, aliases, method values, struct fields and inline literals)
# instead of only following direct calls between named wrappers.
_FUNC_VALUE_ATTACKS: dict[str, str] = {
    "funcvalue-plain": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error { return runone(spawn, os.Getenv("CHESS_TUI_ENGINE")) }
""",
    "funcvalue-literal-inline": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func FallbackEngine() error {
	return runone(func(p string) error { return exec.Command(p).Run() }, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-alias": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	fn := spawn
	return runone(fn, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-method": """package legacy

import (
	"os"
	"os/exec"
)

type holder struct{}

func (h holder) run(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	var h holder
	return h.run(spawn, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-field": """package legacy

import (
	"os"
	"os/exec"
)

type holder struct{ f func(string) error }

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	var h holder
	h.f = spawn
	return runone(h.f, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-two-level": """package legacy

import (
	"os"
	"os/exec"
)

func outer(f func(string) error, p string) error { return runone(f, p) }

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error { return outer(spawn, os.Getenv("CHESS_TUI_ENGINE")) }
""",
    "funcvalue-args-index": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, args []string) error { return f(args[0]) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	return runone(spawn, []string{os.Getenv("CHESS_TUI_ENGINE")})
}
""",
}

_FUNC_VALUE_CROSSFILE: dict[str, tuple[str, dict[str, str]]] = {
    "funcvalue-crossfile": (
        """package legacy

import "os"

func FallbackEngine() error { return runone(spawn, os.Getenv("CHESS_TUI_ENGINE")) }
""",
        {
            "verbs.go": """package legacy

func runone(f func(string) error, p string) error { return f(p) }
""",
            "spawn.go": """package legacy

import "os/exec"

func spawn(p string) error { return exec.Command(p).Run() }
""",
        },
    ),
}

# --- attacker family: function value resolved by *shape* --------------------
# Detection must not depend on the written shape of the argument: a wrapper value
# held in a slice / array / map literal, accumulated with ``append``, produced by
# an inline ``[]func(…){}[i]`` or returned by a factory helper (``pick()``) all
# resolve to the same process sink at run time and must be intercepted.
_FUNC_VALUE_SHAPE_ATTACKS: dict[str, str] = {
    "funcvalue-slice-element": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	handlers := []func(string) error{spawn}
	return runone(handlers[0], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-array-element": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	handlers := [2]func(string) error{spawn, spawn}
	return runone(handlers[1], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-map-element": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	handlers := map[string]func(string) error{"boot": spawn}
	return runone(handlers["boot"], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-append-element": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	var handlers []func(string) error
	handlers = append(handlers, spawn)
	return runone(handlers[0], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-inline-index": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func FallbackEngine() error {
	return runone([]func(string) error{func(p string) error { return exec.Command(p).Run() }}[0], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-factory-return": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func pick() func(string) error { return spawn }

func FallbackEngine() error {
	return runone(pick(), os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-factory-chain": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func inner() func(string) error { return spawn }

func pick() func(string) error { return inner() }

func FallbackEngine() error {
	return runone(pick(), os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-struct-literal-field": """package legacy

import (
	"os"
	"os/exec"
)

type holder struct{ f func(string) error }

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	h := holder{f: spawn}
	return runone(h.f, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-alias-of-element": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	handlers := []func(string) error{spawn}
	fn := handlers[0]
	return runone(fn, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-var-declared-container": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	var handlers []func(string) error = []func(string) error{spawn}
	return runone(handlers[0], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-var-declared-field": """package legacy

import (
	"os"
	"os/exec"
)

type holder struct{ f func(string) error }

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	var h holder = holder{f: spawn}
	return runone(h.f, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-var-declared-callback": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	var fn func(string) error = spawn
	return runone(fn, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-range-element": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	handlers := []func(string) error{spawn}
	for _, fn := range handlers {
		return runone(fn, os.Getenv("CHESS_TUI_ENGINE"))
	}
	return nil
}
""",
    "funcvalue-range-map": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	handlers := map[string]func(string) error{"boot": spawn}
	for _, fn := range handlers {
		return runone(fn, os.Getenv("CHESS_TUI_ENGINE"))
	}
	return nil
}
""",
    "funcvalue-range-inline": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func FallbackEngine() error {
	for _, fn := range []func(string) error{func(p string) error { return exec.Command(p).Run() }} {
		return runone(fn, os.Getenv("CHESS_TUI_ENGINE"))
	}
	return nil
}
""",
    # --- container *producers* other than a composite literal / append --------
    # The container that holds the wrapper may itself be built by a factory
    # return, ``make`` + index assignment, map index assignment, a container
    # alias or a nested factory; element resolution must not depend on the
    # producer, otherwise the higher-order helper's parameter never resolves.
    "funcvalue-container-factory-return": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func table() []func(string) error { return []func(string) error{spawn} }

func FallbackEngine() error {
	return runone(table()[0], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-container-factory-chain": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func inner() []func(string) error { return []func(string) error{spawn} }

func outer() []func(string) error { return inner() }

func FallbackEngine() error {
	return runone(outer()[0], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-make-index-assign": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	handlers := make([]func(string) error, 1)
	handlers[0] = spawn
	return runone(handlers[0], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-var-make-index-assign": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	var handlers []func(string) error
	handlers = make([]func(string) error, 1)
	handlers[0] = spawn
	return runone(handlers[0], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-map-index-assign": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	handlers := map[string]func(string) error{}
	handlers["run"] = spawn
	return runone(handlers["run"], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-map-index-loopkey": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	handlers := map[string]func(string) error{}
	for _, k := range []string{"run"} {
		handlers[k] = spawn
	}
	return runone(handlers["run"], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-container-alias-of-factory": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func table() []func(string) error { return []func(string) error{spawn} }

func FallbackEngine() error {
	handlers := table()
	return runone(handlers[0], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-range-container-factory": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func table() []func(string) error { return []func(string) error{spawn} }

func FallbackEngine() error {
	for _, fn := range table() {
		return runone(fn, os.Getenv("CHESS_TUI_ENGINE"))
	}
	return nil
}
""",
    "funcvalue-struct-field-index-assign": """package legacy

import (
	"os"
	"os/exec"
)

type registry struct{ fns []func(string) error }

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	r := registry{fns: make([]func(string) error, 1)}
	r.fns[0] = spawn
	return runone(r.fns[0], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
}

_FUNC_VALUE_SHAPE_CROSSFILE: dict[str, tuple[str, dict[str, str]]] = {
    "funcvalue-crossfile-factory": (
        """package legacy

import "os"

func FallbackEngine() error { return runone(pick(), os.Getenv("CHESS_TUI_ENGINE")) }
""",
        {
            "runone.go": """package legacy

func runone(f func(string) error, p string) error { return f(p) }
""",
            "spawn.go": """package legacy

import "os/exec"

func spawn(p string) error { return exec.Command(p).Run() }

func pick() func(string) error { return spawn }
""",
        },
    ),
    "funcvalue-crossfile-container-factory": (
        """package legacy

import "os"

func FallbackEngine() error { return runone(table()[0], os.Getenv("CHESS_TUI_ENGINE")) }
""",
        {
            "runone.go": """package legacy

func runone(f func(string) error, p string) error { return f(p) }
""",
            "spawn.go": """package legacy

import "os/exec"

func spawn(p string) error { return exec.Command(p).Run() }

func table() []func(string) error { return []func(string) error{spawn} }
""",
        },
    ),
    "funcvalue-crossfile-make-index": (
        """package legacy

import "os"

func FallbackEngine() error { return runone(register()[0], os.Getenv("CHESS_TUI_ENGINE")) }
""",
        {
            "runone.go": """package legacy

func runone(f func(string) error, p string) error { return f(p) }
""",
            "handlers.go": """package legacy

import "os/exec"

func spawn(p string) error { return exec.Command(p).Run() }

func register() []func(string) error {
	handlers := make([]func(string) error, 1)
	handlers[0] = spawn
	return handlers
}
""",
        },
    ),
}

# --- attacker family: function value resolved by *spelling* -----------------
# The *spelling* of a function value must not matter either: a named function
# type conversion (``launcher(spawn)``), a type assertion out of an empty
# interface (``box.(func(string) error)``) and a selector on a composite-literal
# receiver (``registry{fn: spawn}.fn`` / ``reg{}.launch`` / ``reg{}.pick()``) all
# denote the same run-time wrapper and must resolve to the process sink.  A
# conversion runs no user code, so it preserves the converted function value.
_FUNC_VALUE_SPELLING_ATTACKS: dict[str, str] = {
    "funcvalue-named-type-conversion": """package legacy

import (
	"os"
	"os/exec"
)

type launcher func(string) error

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error { return runone(launcher(spawn), os.Getenv("CHESS_TUI_ENGINE")) }
""",
    "funcvalue-conversion-into-alias": """package legacy

import (
	"os"
	"os/exec"
)

type launcher func(string) error

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	t := launcher(spawn)
	return runone(t, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-type-assertion": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	var box any = spawn
	t := box.(func(string) error)
	return runone(t, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-assert-named-type": """package legacy

import (
	"os"
	"os/exec"
)

type launcher func(string) error

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	var box any = spawn
	return runone(box.(launcher), os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-anon-func-type-conversion": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	var box any = spawn
	t := box.(func(string) error)
	return runone(func(string) error(t), os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-literal-field": """package legacy

import (
	"os"
	"os/exec"
)

type registry struct{ fn func(string) error }

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	return runone(registry{fn: spawn}.fn, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-literal-method-value": """package legacy

import (
	"os"
	"os/exec"
)

type reg struct{}

func runone(f func(string) error, p string) error { return f(p) }

func (reg) launch(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	return runone(reg{}.launch, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-literal-method-factory": """package legacy

import (
	"os"
	"os/exec"
)

type reg struct{}

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func (reg) pick() func(string) error { return spawn }

func FallbackEngine() error {
	return runone(reg{}.pick(), os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-conversion-of-method": """package legacy

import (
	"os"
	"os/exec"
)

type launcher func(string) error
type reg struct{}

func runone(f func(string) error, p string) error { return f(p) }

func (reg) launch(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	return runone(launcher(reg{}.launch), os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-literal-field-method": """package legacy

import (
	"os"
	"os/exec"
)

type reg struct{}
type holder struct{ run func(string) error }

func runone(f func(string) error, p string) error { return f(p) }

func (reg) launch(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	return runone(holder{run: reg{}.launch}.run, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-nested-literal-field": """package legacy

import (
	"os"
	"os/exec"
)

type registry struct{ fn func(string) error }
type outer struct{ inner registry }

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	return runone(outer{inner: registry{fn: spawn}}.inner.fn, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
}

_FUNC_VALUE_SPELLING_CROSSFILE: dict[str, tuple[str, dict[str, str]]] = {
    "funcvalue-crossfile-conversion": (
        """package legacy

import "os"

func FallbackEngine() error { return runone(launcher(spawn), os.Getenv("CHESS_TUI_ENGINE")) }
""",
        {
            "runone.go": """package legacy

type launcher func(string) error

func runone(f func(string) error, p string) error { return f(p) }
""",
            "spawn.go": """package legacy

import "os/exec"

func spawn(p string) error { return exec.Command(p).Run() }
""",
        },
    ),
    "funcvalue-crossfile-literal-method": (
        """package legacy

import "os"

func FallbackEngine() error { return runone(reg{}.launch, os.Getenv("CHESS_TUI_ENGINE")) }
""",
        {
            "runone.go": """package legacy

type reg struct{}

func runone(f func(string) error, p string) error { return f(p) }
""",
            "spawn.go": """package legacy

import "os/exec"

func (reg) launch(p string) error { return exec.Command(p).Run() }
""",
        },
    ),
}

# --- function-value *transport* shapes: channels and local closure factories --
# A wrapper function value reaches a higher-order helper through *any* data
# channel, not only a plain assignment: a channel send/receive (``ch <- spawn``
# / ``<-ch``) and a *local* closure whose body returns the wrapper
# (``makeLauncher := func() func(string) error { return spawn }``) deliver the
# same value and must resolve to the process sink just like a bare name.  A
# channel behaves as a container and a local closure as a value factory.
_FUNC_VALUE_TRANSPORT_ATTACKS: dict[str, str] = {
    "funcvalue-channel-send-receive": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	ch := make(chan func(string) error, 1)
	ch <- spawn
	return runone(<-ch, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-channel-goroutine": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	ch := make(chan func(string) error)
	go func() { ch <- spawn }()
	return runone(<-ch, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-channel-field": """package legacy

import (
	"os"
	"os/exec"
)

type hub struct{ ch chan func(string) error }

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	h := hub{ch: make(chan func(string) error, 1)}
	h.ch <- spawn
	return runone(<-h.ch, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-channel-receive-alias": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	ch := make(chan func(string) error, 1)
	ch <- spawn
	t := <-ch
	return runone(t, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-local-closure-factory": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	makeLauncher := func() func(string) error { return spawn }
	return runone(makeLauncher(), os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-local-closure-var": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	var makeL = func() func(string) error { return spawn }
	return runone(makeL(), os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-local-closure-value": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	mk := func() func(string) error { return spawn }
	launch := mk()
	return runone(launch, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-channel-factory-call": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	mk := func() func(string) error { return spawn }
	ch := make(chan func() func(string) error, 1)
	ch <- mk
	return runone((<-ch)(), os.Getenv("CHESS_TUI_ENGINE"))
}
""",
}

_FUNC_VALUE_TRANSPORT_CROSSFILE: dict[str, tuple[str, dict[str, str]]] = {
    "funcvalue-crossfile-channel": (
        """package legacy

import "os"

func FallbackEngine() error {
	ch := make(chan func(string) error, 1)
	ch <- spawn
	return runone(<-ch, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
        {
            "runone.go": """package legacy

func runone(f func(string) error, p string) error { return f(p) }
""",
            "spawn.go": """package legacy

import "os/exec"

func spawn(p string) error { return exec.Command(p).Run() }
""",
        },
    ),
    "funcvalue-crossfile-local-closure": (
        """package legacy

import "os"

func FallbackEngine() error {
	mk := func() func(string) error { return spawn }
	return runone(mk(), os.Getenv("CHESS_TUI_ENGINE"))
}
""",
        {
            "runone.go": """package legacy

func runone(f func(string) error, p string) error { return f(p) }
""",
            "spawn.go": """package legacy

import "os/exec"

func spawn(p string) error { return exec.Command(p).Run() }
""",
        },
    ),
}

# --- attacker family: function-value transport by *addressing* (r11 bypass) --
# The r11 verifier bypassed the transport check by changing how the launcher was
# *addressed* -- an alias of the channel, a select receive, a map-index channel,
# a pointer-aliased field, a slice reached through a channel, a variadic helper
# argument, a variadic factory and a closure factory held in a slice.  The value
# flow is the same in every case: a wrapper function value delivered to the
# process through an equivalent addressing spelling.
_FUNC_VALUE_INDIRECTION_ATTACKS: dict[str, str] = {
    "funcvalue-channel-alias": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	ch := make(chan func(string) error, 1)
	relay := ch
	relay <- spawn
	return runone(<-ch, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-channel-select-receive": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	ch := make(chan func(string) error, 1)
	ch <- spawn
	var f func(string) error
	select {
	case f = <-ch:
	default:
	}
	return runone(f, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-map-index-channel": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	m := map[string]chan func(string) error{"a": make(chan func(string) error, 1)}
	m["a"] <- spawn
	return runone(<-m["a"], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-channel-field-pointer-alias": """package legacy

import (
	"os"
	"os/exec"
)

type hub struct{ ch chan func(string) error }

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	h := hub{ch: make(chan func(string) error, 1)}
	q := &h
	q.ch <- spawn
	return runone(<-h.ch, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-channel-slice-index": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	ch := make(chan []func(string) error, 1)
	ch <- []func(string) error{spawn}
	fs := <-ch
	return runone(fs[0], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-variadic-index-call": """package legacy

import (
	"os"
	"os/exec"
)

func runone(p string, fns ...func(string) error) error { return fns[0](p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	return runone(os.Getenv("CHESS_TUI_ENGINE"), spawn)
}
""",
    "funcvalue-variadic-factory": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func gather(fns ...func(string) error) func(string) error { return fns[0] }

func FallbackEngine() error {
	return runone(gather(spawn), os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-closure-slice-index-call": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	fns := []func() func(string) error{func() func(string) error { return spawn }}
	return runone(fns[0](), os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-channel-alias-chain": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	ch := make(chan func(string) error, 1)
	a := ch
	b := a
	b <- spawn
	return runone(<-ch, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-channel-slice-slot": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	slots := []chan func(string) error{make(chan func(string) error, 1)}
	slots[0] <- spawn
	return runone(<-slots[0], os.Getenv("CHESS_TUI_ENGINE"))
}
""",
    "funcvalue-variadic-spread": """package legacy

import (
	"os"
	"os/exec"
)

func runone(p string, fns ...func(string) error) error { return fns[0](p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	launchers := []func(string) error{spawn}
	return runone(os.Getenv("CHESS_TUI_ENGINE"), launchers...)
}
""",
    "funcvalue-select-receive-decl": """package legacy

import (
	"os"
	"os/exec"
)

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func FallbackEngine() error {
	ch := make(chan func(string) error, 1)
	ch <- spawn
	select {
	case f := <-ch:
		return runone(f, os.Getenv("CHESS_TUI_ENGINE"))
	default:
	}
	return nil
}
""",
}

_FUNC_VALUE_INDIRECTION_CROSSFILE: dict[str, tuple[str, dict[str, str]]] = {
    "funcvalue-crossfile-channel-alias": (
        """package legacy

import "os"

func FallbackEngine() error {
	ch := make(chan func(string) error, 1)
	relay := ch
	relay <- spawn
	return runone(<-ch, os.Getenv("CHESS_TUI_ENGINE"))
}
""",
        {
            "runone.go": """package legacy

func runone(f func(string) error, p string) error { return f(p) }
""",
            "spawn.go": """package legacy

import "os/exec"

func spawn(p string) error { return exec.Command(p).Run() }
""",
        },
    ),
    "funcvalue-crossfile-variadic": (
        """package legacy

import "os"

func FallbackEngine() error {
	return runone(os.Getenv("CHESS_TUI_ENGINE"), spawn)
}
""",
        {
            "runone.go": """package legacy

func runone(p string, fns ...func(string) error) error { return fns[0](p) }
""",
            "spawn.go": """package legacy

import "os/exec"

func spawn(p string) error { return exec.Command(p).Run() }
""",
        },
    ),
}

# --- negative controls: legitimate behaviour must not be flagged -----------
_CONTROLS = {
    "allowed-uci-engine": """package uci

import "os/exec"

func bestMove(enginePath, fen string) (string, error) {
	cmd := exec.Command(enginePath)
	_ = fen
	return "e2e4", cmd.Start()
}
""",
    "engine-path-with-args": """package uci

import (
	"os/exec"
	"strings"
)

func (b *Bot) GetMove(fen string) (string, error) {
	parts := strings.Fields(b.EnginePath)
	var cmd *exec.Cmd
	if len(parts) == 0 {
		cmd = exec.Command(b.EnginePath)
	} else {
		cmd = exec.Command(parts[0], parts[1:]...)
	}
	_ = fen
	return "e2e4", cmd.Start()
}
""",
    "config-field-program": """package runner

import (
	"os"
	"os/exec"
)

type settings struct{ EnginePath string }

func start(cfgPath string) error {
	blob, err := os.ReadFile(cfgPath)
	if err != nil {
		return err
	}
	cfg := settings{EnginePath: string(blob)}
	return exec.Command(cfg.EnginePath).Start()
}
""",
    "nested-config-field": """package runner

import (
	"os"
	"os/exec"
)

type inner struct{ Program string }
type outer struct{ In inner }

func start(cfgPath string) error {
	blob, err := os.ReadFile(cfgPath)
	if err != nil {
		return err
	}
	o := outer{In: inner{Program: string(blob)}}
	return exec.Command(o.In.Program).Start()
}
""",
    "wrapper-called-with-literal": """package uci

import "os/exec"

func run(program string) error { return exec.Command(program).Run() }

func start() error { return run("/usr/bin/stockfish") }
""",
    "env-field-not-exec": """package logging

import "os"

type plan struct{ Program string }

func describe() string {
	p := plan{Program: os.Getenv("CHESS_TUI_ENGINE")}
	return "engine candidate: " + p.Program
}
""",
    "env-field-clean-literal": """package uci

import (
	"os"
	"os/exec"
)

type plan struct{ Program string }

func bestMove() error {
	_ = os.Getenv("CHESS_TUI_ENGINE") // read but never used to name the process
	p := plan{Program: "/usr/bin/stockfish"}
	return exec.Command(p.Program).Run()
}
""",
    "plain-string-building": """package config

import "strings"

func displayPath(base, leaf string) string {
	return strings.Join([]string{base, leaf}, "/")
}
""",
    "byte-dimensions": """package chess

func boardShape() int {
	dims := []byte{8, 8}
	return len(dims) + int(dims[0])
}
""",
    "aliased-engine-config": """package uci

import (
	ex "os/exec"
	"os"
)

func bestMove(enginePath, fen string) (string, error) {
	_ = os.Getenv("CHESS_TUI_ENGINE") // an env read that never names the process
	cmd := ex.Command(enginePath)
	_ = fen
	return "e2e4", cmd.Start()
}
""",
    "crossfile-read-display": """package report

import (
	"os"
	"strings"
)

func load(path string) ([]byte, error) {
	return os.ReadFile(path)
}

func show(path string) (string, error) {
	blob, err := load(path)
	if err != nil {
		return "", err
	}
	return strings.ToUpper(string(blob)), nil
}
""",
    "wrapper-called-with-config": """package uci

import "os/exec"

func run(program string) error { return exec.Command(program).Run() }

func start(enginePath string) error { return run(enginePath) }
""",
    "funcvalue-literal-payload": """package uci

import "os/exec"

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func start() error { return runone(spawn, "/usr/bin/stockfish") }
""",
    "funcvalue-config-param": """package uci

import "os/exec"

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func start(enginePath string) error { return runone(spawn, enginePath) }
""",
    "funcvalue-nonwrapper": """package report

import (
	"os"
	"strings"
)

func each(value string, fn func(string) string) string { return fn(value) }

func upper(s string) string { return strings.ToUpper(s) }

func describe() string { return each(os.Getenv("CHESS_TUI_ENGINE"), upper) }
""",
    "funcvalue-container-literal-payload": """package uci

import "os/exec"

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func start() error {
	handlers := []func(string) error{spawn}
	return runone(handlers[0], "/usr/bin/stockfish")
}
""",
    "funcvalue-container-config-param": """package uci

import "os/exec"

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func start(enginePath string) error {
	handlers := []func(string) error{spawn}
	return runone(handlers[0], enginePath)
}
""",
    "funcvalue-nonwrapper-container": """package report

import (
	"os"
	"strings"
)

func each(value string, fn func(string) string) string { return fn(value) }

func upper(s string) string { return strings.ToUpper(s) }

func describe() string {
	fns := []func(string) string{upper}
	return each(os.Getenv("CHESS_TUI_ENGINE"), fns[0])
}
""",
    "funcvalue-var-container-literal-payload": """package uci

import "os/exec"

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func start() error {
	var handlers []func(string) error = []func(string) error{spawn}
	return runone(handlers[0], "/usr/bin/stockfish")
}
""",
    "funcvalue-range-config-param": """package uci

import "os/exec"

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func start(enginePath string) error {
	handlers := []func(string) error{spawn}
	for _, fn := range handlers {
		return runone(fn, enginePath)
	}
	return nil
}
""",
    "funcvalue-range-nonwrapper": """package report

import (
	"os"
	"strings"
)

func each(value string, fn func(string) string) string { return fn(value) }

func upper(s string) string { return strings.ToUpper(s) }

func describe() string {
	fns := []func(string) string{upper}
	for _, fn := range fns {
		return each(os.Getenv("CHESS_TUI_ENGINE"), fn)
	}
	return ""
}
""",
    "funcvalue-container-factory-literal-payload": """package uci

import "os/exec"

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func table() []func(string) error { return []func(string) error{spawn} }

func start() error { return runone(table()[0], "/usr/bin/stockfish") }
""",
    "funcvalue-make-config-param": """package uci

import "os/exec"

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func start(enginePath string) error {
	handlers := make([]func(string) error, 1)
	handlers[0] = spawn
	return runone(handlers[0], enginePath)
}
""",
    "funcvalue-map-index-config-param": """package uci

import "os/exec"

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func start(enginePath string) error {
	handlers := map[string]func(string) error{}
	handlers["run"] = spawn
	return runone(handlers["run"], enginePath)
}
""",
    "funcvalue-make-nonwrapper": """package report

import (
	"os"
	"strings"
)

func each(value string, fn func(string) string) string { return fn(value) }

func upper(s string) string { return strings.ToUpper(s) }

func describe() string {
	handlers := make([]func(string) string, 1)
	handlers[0] = upper
	return each(os.Getenv("CHESS_TUI_ENGINE"), handlers[0])
}
""",
    "funcvalue-container-of-nonwrapper-map": """package report

import (
	"os"
	"strings"
)

func each(value string, fn func(string) string) string { return fn(value) }

func upper(s string) string { return strings.ToUpper(s) }

func describe() string {
	handlers := map[string]func(string) string{}
	handlers["run"] = upper
	return each(os.Getenv("CHESS_TUI_ENGINE"), handlers["run"])
}
""",
    "funcvalue-conversion-nonwrapper": """package report

import (
	"os"
	"strings"
)

type mapper func(string) string

func each(value string, fn func(string) string) string { return fn(value) }

func upper(s string) string { return strings.ToUpper(s) }

func describe() string { return each(os.Getenv("CHESS_TUI_ENGINE"), mapper(upper)) }
""",
    "funcvalue-literal-field-nonwrapper": """package report

import (
	"os"
	"strings"
)

type holder struct{ run func(string) string }

func each(value string, fn func(string) string) string { return fn(value) }

func upper(s string) string { return strings.ToUpper(s) }

func describe() string {
	return each(os.Getenv("CHESS_TUI_ENGINE"), holder{run: upper}.run)
}
""",
    "funcvalue-assert-nonwrapper": """package report

import (
	"os"
	"strings"
)

func each(value string, fn func(string) string) string { return fn(value) }

func upper(s string) string { return strings.ToUpper(s) }

func describe() string {
	var box any = upper
	return each(os.Getenv("CHESS_TUI_ENGINE"), box.(func(string) string))
}
""",
    "funcvalue-literal-method-nonwrapper": """package report

import (
	"os"
	"strings"
)

type reg struct{}

func each(value string, fn func(string) string) string { return fn(value) }

func upper(s string) string { return strings.ToUpper(s) }

func (reg) pick() func(string) string { return upper }

func describe() string {
	return each(os.Getenv("CHESS_TUI_ENGINE"), reg{}.pick())
}
""",
    "funcvalue-conversion-literal-payload": """package uci

import "os/exec"

type launcher func(string) error

func runone(f func(string) error, p string) error { return f(p) }

func spawn(p string) error { return exec.Command(p).Run() }

func start() error { return runone(launcher(spawn), "/usr/bin/stockfish") }
""",
    "funcvalue-channel-string-nonwrapper": """package legacy

import "os/exec"

func Wait(ch <-chan string) string { return <-ch }

func start(enginePath string) error {
	ch := make(chan string, 1)
	ch <- "uci"
	_ = Wait(ch)
	cmd := exec.Command(enginePath)
	_ = os.Getenv("HOME")
	return cmd.Start()
}
""",
    "funcvalue-local-closure-nonwrapper": """package report

import "os/exec"

func describe(enginePath string) error {
	mk := func() string { return "uci" }
	_ = mk()
	cmd := exec.Command(enginePath)
	_ = os.Getenv("HOME")
	return cmd.Start()
}
""",
    "funcvalue-local-closure-nonwrapper-func": """package report

import (
	"os"
	"os/exec"
	"strings"
)

func each(value string, fn func(string) string) string { return fn(value) }

func upper(s string) string { return strings.ToUpper(s) }

func describe(enginePath string) error {
	mk := func() func(string) string { return upper }
	_ = each(os.Getenv("CHESS_TUI_ENGINE"), mk())
	cmd := exec.Command(enginePath)
	return cmd.Start()
}
""",
    "funcvalue-channel-type-forms": """package legacy

import "os/exec"

func Send(tx chan<- string, value string) { tx <- value }

func Recv(rx <-chan string) string { return <-rx }

func start(enginePath string) error {
	ch := make(chan string, 1)
	Send(ch, "uci")
	_ = Recv(ch)
	cmd := exec.Command(enginePath)
	return cmd.Start()
}
""",
}

# Legitimate look-alikes of the r11 *addressing* shapes: none carries a wrapper
# function value into the process sink (the channels / slices / variadics hold
# plain data), so none may be flagged.
_CONTROLS.update(
    {
        "funcvalue-channel-alias-string": """package report

import "os/exec"

func relayString(enginePath string) error {
	ch := make(chan string, 1)
	relay := ch
	relay <- "uci"
	_ = <-ch
	cmd := exec.Command(enginePath)
	return cmd.Start()
}
""",
        "funcvalue-select-string": """package report

import "os/exec"

func selectString(enginePath string) error {
	ch := make(chan string, 1)
	ch <- "uci"
	var s string
	select {
	case s = <-ch:
	default:
	}
	_ = s
	cmd := exec.Command(enginePath)
	return cmd.Start()
}
""",
        "funcvalue-map-index-string-channel": """package report

import "os/exec"

func mapChannel(enginePath string) error {
	m := map[string]chan string{"a": make(chan string, 1)}
	m["a"] <- "uci"
	_ = <-m["a"]
	cmd := exec.Command(enginePath)
	return cmd.Start()
}
""",
        "funcvalue-variadic-strings": """package report

import "os/exec"

func joinAll(parts ...string) string { return parts[0] }

func variadicString(enginePath string) error {
	_ = joinAll("u", "ci")
	cmd := exec.Command(enginePath)
	return cmd.Start()
}
""",
        "funcvalue-slice-of-string-func": """package report

import "os/exec"

func pickString() func() string { return func() string { return "uci" } }

func sliceString(enginePath string) error {
	fns := []func() string{pickString}
	_ = fns[0]()
	cmd := exec.Command(enginePath)
	return cmd.Start()
}
""",
    }
)

_EMBED_SCRIPT = """#!/bin/sh
exec cargo run --release -- "$@"
"""


def _load_rules() -> list[dict]:
    for candidate in (HERE.parent / "config.json", HERE / "config.json"):
        if candidate.is_file():
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rules = payload.get("policy", {}).get("deceptive_fallback_rules")
            if isinstance(rules, list) and rules:
                return rules
    # Hydrated fallback mirroring the shipped policy.
    return [
        {"id": "BATCH-F2P-0001-DECEPT-002", "kind": "literal", "target": "cargo ", "paths": ["internal/**/*.go"]},
        {"id": "BATCH-F2P-0001-DECEPT-003", "kind": "literal", "target": "target/release", "paths": ["internal/**/*.go"]},
        {"id": "BATCH-P2P-0001-DECEPTIVE-001", "kind": "literal", "target": 'exec.Command("chess-tui', "paths": ["internal/**/*.go"]},
        {"id": "BATCH-P2P-0001-DECEPTIVE-005", "kind": "runtime", "target": "opaque-payload-to-process-sink", "paths": ["internal/**/*.go"]},
    ]


def _rule_ids(rules: list[dict]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for row in rules:
        if row.get("target") in {"cargo ", "target/release"}:
            lookup[row["target"]] = row["id"]
        if row.get("kind") == "runtime":
            lookup["runtime"] = row["id"]
        if isinstance(row.get("target"), str) and row["target"].startswith('exec.Command("chess-tui'):
            lookup["program"] = row["id"]
    return lookup


def _scan(source: str, rules: list[dict], *, extra: dict[str, str] | None = None, base: str = "internal/legacy") -> list[dict]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / base
        target.mkdir(parents=True)
        (target / "legacy.go").write_text(source, encoding="utf-8")
        for name, content in (extra or {}).items():
            (target / name).write_text(content, encoding="utf-8")
        return scan_residue_rules(root, rules)


def _missing(hits: list[dict], required: set[str]) -> set[str]:
    return required - {hit["rule_id"] for hit in hits}


def main() -> int:
    rules = _load_rules()
    ids = _rule_ids(rules)
    failures: list[str] = []

    literal_ids = {ids["cargo "], ids["target/release"]}
    for name, source in _ATTACKS.items():
        hits = _scan(source, rules)
        missing = _missing(hits, literal_ids)
        if missing:
            failures.append(f"literal attack '{name}' not intercepted (missing {sorted(missing)}); hits={hits}")

    for name, source in _OPAQUE_ATTACKS.items():
        extra = {"run.sh": _EMBED_SCRIPT} if "embed" in name else None
        hits = _scan(source, rules, extra=extra)
        if not hits:
            failures.append(f"opaque attack '{name}' not intercepted; hits={hits}")

    for name, source in _PROGRAM_ATTACKS.items():
        hits = _scan(source, rules)
        if not hits:
            failures.append(f"program attack '{name}' not intercepted; hits={hits}")

    for name, (source, extra) in _CROSSFILE_ATTACKS.items():
        hits = _scan(source, rules, extra=extra)
        if not hits:
            failures.append(f"cross-file attack '{name}' not intercepted; hits={hits}")

    for name, source in _ALIAS_ATTACKS.items():
        hits = _scan(source, rules, base="internal/legacy")
        if not hits:
            failures.append(f"alias attack '{name}' not intercepted; hits={hits}")

    for name, source in _FOLD_ATTACKS.items():
        hits = _scan(source, rules)
        if not hits:
            failures.append(f"constant-fold attack '{name}' not intercepted; hits={hits}")

    for name, (source, extra) in _WRAPPER_CHAIN_ATTACKS.items():
        hits = _scan(source, rules, extra=extra)
        if not hits:
            failures.append(f"wrapper-chain attack '{name}' not intercepted; hits={hits}")

    for name, source in _FUNC_VALUE_ATTACKS.items():
        hits = _scan(source, rules)
        if not hits:
            failures.append(f"function-value attack '{name}' not intercepted; hits={hits}")

    for name, (source, extra) in _FUNC_VALUE_CROSSFILE.items():
        hits = _scan(source, rules, extra=extra)
        if not hits:
            failures.append(f"function-value cross-file attack '{name}' not intercepted; hits={hits}")

    for name, source in _FUNC_VALUE_SHAPE_ATTACKS.items():
        hits = _scan(source, rules)
        if not hits:
            failures.append(f"function-value shape attack '{name}' not intercepted; hits={hits}")

    for name, (source, extra) in _FUNC_VALUE_SHAPE_CROSSFILE.items():
        hits = _scan(source, rules, extra=extra)
        if not hits:
            failures.append(f"function-value shape cross-file attack '{name}' not intercepted; hits={hits}")

    for name, source in _FUNC_VALUE_SPELLING_ATTACKS.items():
        hits = _scan(source, rules)
        if not hits:
            failures.append(f"function-value spelling attack '{name}' not intercepted; hits={hits}")

    for name, (source, extra) in _FUNC_VALUE_SPELLING_CROSSFILE.items():
        hits = _scan(source, rules, extra=extra)
        if not hits:
            failures.append(f"function-value spelling cross-file attack '{name}' not intercepted; hits={hits}")

    for name, source in _FUNC_VALUE_TRANSPORT_ATTACKS.items():
        hits = _scan(source, rules)
        if not hits:
            failures.append(f"function-value transport attack '{name}' not intercepted; hits={hits}")

    for name, (source, extra) in _FUNC_VALUE_TRANSPORT_CROSSFILE.items():
        hits = _scan(source, rules, extra=extra)
        if not hits:
            failures.append(f"function-value transport cross-file attack '{name}' not intercepted; hits={hits}")

    for name, source in _FUNC_VALUE_INDIRECTION_ATTACKS.items():
        hits = _scan(source, rules)
        if not hits:
            failures.append(f"function-value indirection attack '{name}' not intercepted; hits={hits}")

    for name, (source, extra) in _FUNC_VALUE_INDIRECTION_CROSSFILE.items():
        hits = _scan(source, rules, extra=extra)
        if not hits:
            failures.append(f"function-value indirection cross-file attack '{name}' not intercepted; hits={hits}")

    for name, source in _FIELD_ATTACKS.items():
        hits = _scan(source, rules)
        if not hits:
            failures.append(f"field-pass attack '{name}' not intercepted; hits={hits}")

    # A fallback under an excluded-looking directory is still scanned.
    for name, base in {"build-dir-subprocess": "build/legacy", "dist-dir-subprocess": "dist/legacy"}.items():
        hits = _scan(_PROGRAM_ATTACKS["variable-program-name"], rules, base=base)
        if not hits:
            failures.append(f"excluded-dir attack '{name}' not intercepted; hits={hits}")

    for name, source in _CONTROLS.items():
        hits = _scan(source, rules)
        if hits:
            failures.append(f"control '{name}' falsely flagged: {hits}")

    # Go modules declare dependencies as ``require path version`` (also inside a
    # ``require ( … )`` block); the dependency rules must see that shape.
    with tempfile.TemporaryDirectory() as temporary:
        go_mod = Path(temporary) / "go.mod"
        go_mod.write_text(
            "module github.com/thomas-mauran/chess-tui\n\ngo 1.25.0\n\nrequire (\n"
            "\tgithub.com/BurntSushi/toml v1.6.0\n"
            "\tgithub.com/notnil/chess v1.10.0\n"
            "\tgithub.com/thomas-mauran/chess-tui-ffi v0.0.0\n)\n",
            encoding="utf-8",
        )
        if not _dependency_contains(go_mod, "github.com/thomas-mauran/chess-tui-ffi"):
            failures.append("go.mod require-block dependency not detected")
        if _dependency_contains(go_mod, "github.com/example/absent"):
            failures.append("go.mod dependency false positive")
        if _dependency_contains(go_mod, "github.com/thomas-mauran/chess-tui"):
            failures.append("go.mod module path mistaken for a dependency")

    if failures:
        for row in failures:
            print(f"FAIL: {row}", file=sys.stderr)
        return 1
    print(
        "residue self-check passed: "
        f"{len(_ATTACKS)} literal attacks, {len(_OPAQUE_ATTACKS)} opaque attacks, "
        f"{len(_PROGRAM_ATTACKS)} program-name attacks, "
        f"{len(_CROSSFILE_ATTACKS)} cross-file attacks, {len(_ALIAS_ATTACKS)} alias attacks, "
        f"{len(_FOLD_ATTACKS)} constant-fold attacks, "
        f"{len(_WRAPPER_CHAIN_ATTACKS)} wrapper-chain attacks, "
        f"{len(_FUNC_VALUE_ATTACKS)} function-value attacks, "
        f"{len(_FUNC_VALUE_CROSSFILE)} function-value cross-file attacks, "
        f"{len(_FUNC_VALUE_SHAPE_ATTACKS)} function-value shape attacks, "
        f"{len(_FUNC_VALUE_SHAPE_CROSSFILE)} function-value shape cross-file attacks, "
        f"{len(_FUNC_VALUE_SPELLING_ATTACKS)} function-value spelling attacks, "
        f"{len(_FUNC_VALUE_SPELLING_CROSSFILE)} function-value spelling cross-file attacks, "
        f"{len(_FUNC_VALUE_TRANSPORT_ATTACKS)} function-value transport attacks, "
        f"{len(_FUNC_VALUE_TRANSPORT_CROSSFILE)} function-value transport cross-file attacks, "
        f"{len(_FUNC_VALUE_INDIRECTION_ATTACKS)} function-value indirection attacks, "
        f"{len(_FUNC_VALUE_INDIRECTION_CROSSFILE)} function-value indirection cross-file attacks, "
        f"{len(_FIELD_ATTACKS)} field-pass attacks, "
        f"2 excluded-dir attacks, {len(_CONTROLS)} controls clean, "
        "go.mod dependency parsing ok"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/thomas-mauran/chess-tui/internal/config"
	"github.com/thomas-mauran/chess-tui/internal/pgn"
)

const f2pSimplePGN = `[Event "%s"]
[White "W"]
[Black "B"]
[Result "1-0"]

1. e4 e5 2. Nf3 1-0
`

// F2P-025 (R033): the deliverable is a Go-only addition: go.mod declares the
// expected module and dependency set, and the preserved Cargo build files stay
// valid.
func TestF2P025GoOnlyDeliverable(t *testing.T) {
	gomod, err := os.ReadFile("go.mod")
	if err != nil {
		t.Fatalf("read go.mod: %v", err)
	}
	text := string(gomod)
	if !strings.Contains(text, "module github.com/thomas-mauran/chess-tui") {
		t.Fatalf("go.mod missing module declaration:\n%s", text)
	}

	cargo, err := os.ReadFile("Cargo.toml")
	if err != nil {
		t.Fatalf("Cargo.toml should stay valid: %v", err)
	}
	if !strings.Contains(string(cargo), "[package]") || !strings.Contains(string(cargo), "edition = \"2024\"") {
		t.Fatalf("Cargo.toml no longer declares the Rust package:\n%s", string(cargo))
	}
}

// F2P-028 / F2P-047 (R002): run(["--pgn","examples/sample.pgn"]) returns 0
// without initializing a terminal.
func TestF2P028SamplePgnHeadless(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	var stdout, stderr bytes.Buffer
	code := run([]string{"--pgn", filepath.Join("examples", "sample.pgn")}, strings.NewReader(""), &stdout, &stderr)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr.String())
	}
}

// F2P-047 (R002): the same headless startup on the documented entry point.
func TestF2P047HeadlessStartup(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	var stdout, stderr bytes.Buffer
	code := run([]string{"--pgn", filepath.Join("examples", "sample.pgn")}, strings.NewReader(""), &stdout, &stderr)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr.String())
	}
}

// F2P-030 (R050): directory --pgn loads *.pgn in sorted path order.
func TestF2P030PgnDirectorySorted(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "b.pgn"), []byte(strings.Replace(f2pSimplePGN, "%s", "Second", 1)), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "a.pgn"), []byte(strings.Replace(f2pSimplePGN, "%s", "First", 1)), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "ignore.txt"), []byte("not pgn"), 0o644); err != nil {
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	games, err := loadPGN(dir, &stderr)
	if err != nil {
		t.Fatal(err)
	}
	if len(games) != 2 || games[0].Title != "First" || games[1].Title != "Second" {
		t.Fatalf("order = %v", games)
	}
}

// F2P-039 (R050): run returns 1 for an empty directory and for a missing file.
func TestF2P039PgnFailureExit(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	var stdout, stderr bytes.Buffer
	code := run([]string{"--pgn", t.TempDir()}, strings.NewReader(""), &stdout, &stderr)
	if code != 1 {
		t.Fatalf("empty dir exit = %d, want 1", code)
	}
	if strings.TrimSpace(stderr.String()) == "" {
		t.Fatalf("empty dir must report the failure on stderr")
	}
}

// F2P-037 (R050): a missing --pgn file returns 1.
func TestF2P037MissingPgnExit(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	var stdout, stderr bytes.Buffer
	code := run([]string{"--pgn", filepath.Join(t.TempDir(), "missing.pgn")}, strings.NewReader(""), &stdout, &stderr)
	if code != 1 {
		t.Fatalf("missing file exit = %d, want 1", code)
	}
}

// F2P-040 (R032): production code runs with the Rust runtime absent:
// config.Load on a missing file returns defaults and pgn.FromPGNString parses.
func TestF2P040RuntimeIndependenceFromRust(t *testing.T) {
	cfg := config.Load(filepath.Join(t.TempDir(), "missing.toml"))
	if cfg.DisplayMode == nil || *cfg.DisplayMode != "DEFAULT" {
		t.Fatalf("display mode = %v", cfg.DisplayMode)
	}
	if cfg.BotDepth == nil || *cfg.BotDepth != 10 {
		t.Fatalf("bot depth = %v", cfg.BotDepth)
	}

	games, err := pgn.FromPGNString(strings.Replace(f2pSimplePGN, "%s", "Independent", 1))
	if err != nil {
		t.Fatal(err)
	}
	if len(games) != 1 || games[0].Title != "Independent" {
		t.Fatalf("games = %+v", games)
	}
}

package config

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/BurntSushi/toml"
)

// F2P-029 (R039): Persisting a Config whose EnginePath, BotDifficulty and
// LichessToken are nil omits those keys yet writes the remaining fields, and
// decoding the file reproduces the same values.
func TestF2P029PersistOmitsOptionalFields(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")

	cfg := Default()
	cfg.EnginePath = nil
	cfg.BotDifficulty = nil
	cfg.LichessToken = nil

	if err := cfg.Persist(path); err != nil {
		t.Fatalf("persist: %v", err)
	}

	var raw map[string]any
	if _, err := toml.DecodeFile(path, &raw); err != nil {
		t.Fatalf("decode raw: %v", err)
	}
	for _, key := range []string{"engine_path", "bot_difficulty", "lichess_token"} {
		if _, ok := raw[key]; ok {
			t.Fatalf("key %q should be omitted, got %v", key, raw[key])
		}
	}
	for _, key := range []string{"display_mode", "log_level", "bot_depth", "selected_skin_name", "sound_enabled", "animations_enabled"} {
		if _, ok := raw[key]; !ok {
			t.Fatalf("key %q must be present, file=%v", key, raw)
		}
	}

	decoded := Load(path)
	if decoded.DisplayMode == nil || *decoded.DisplayMode != "DEFAULT" {
		t.Fatalf("display_mode = %v", decoded.DisplayMode)
	}
	if decoded.LogLevel == nil || *decoded.LogLevel != "OFF" {
		t.Fatalf("log_level = %v", decoded.LogLevel)
	}
	if decoded.BotDepth == nil || *decoded.BotDepth != 10 {
		t.Fatalf("bot_depth = %v", decoded.BotDepth)
	}
	if decoded.SelectedSkinName == nil || *decoded.SelectedSkinName != "Default" {
		t.Fatalf("selected_skin_name = %v", decoded.SelectedSkinName)
	}
	if decoded.SoundEnabled == nil || !*decoded.SoundEnabled {
		t.Fatalf("sound_enabled = %v", decoded.SoundEnabled)
	}
	if decoded.AnimationsEnabled == nil || *decoded.AnimationsEnabled {
		t.Fatalf("animations_enabled = %v", decoded.AnimationsEnabled)
	}
}

// F2P-048 (R040): depth is an unsigned 8-bit value, and every short flag
// parses identically to its long form.
func TestF2P048ParseArgsDepthBoundsAndShortFlags(t *testing.T) {
	ok, err := ParseArgs([]string{"--depth", "255"})
	if err != nil || ok.Depth == nil || *ok.Depth != 255 {
		t.Fatalf("--depth 255 = %+v err=%v", ok, err)
	}
	if _, err := ParseArgs([]string{"--depth", "256"}); err == nil {
		t.Fatal("--depth 256 must be rejected")
	}
	if _, err := ParseArgs([]string{"--depth", "-1"}); err == nil {
		t.Fatal("--depth -1 must be rejected")
	}

	short, err := ParseArgs([]string{"-e", "/engine", "-d", "12", "-l", "tok", "-p", "g.pgn"})
	if err != nil {
		t.Fatal(err)
	}
	long, err := ParseArgs([]string{"--engine-path", "/engine", "--depth", "12", "--lichess-token", "tok", "--pgn", "g.pgn"})
	if err != nil {
		t.Fatal(err)
	}
	if short.EnginePath != long.EnginePath || short.EnginePath != "/engine" {
		t.Fatalf("engine path: short=%q long=%q", short.EnginePath, long.EnginePath)
	}
	if short.Depth == nil || long.Depth == nil || *short.Depth != *long.Depth || *short.Depth != 12 {
		t.Fatalf("depth: short=%v long=%v", short.Depth, long.Depth)
	}
	if short.LichessToken == nil || long.LichessToken == nil || *short.LichessToken != *long.LichessToken {
		t.Fatalf("token: short=%v long=%v", short.LichessToken, long.LichessToken)
	}
	if short.Pgn == nil || long.Pgn == nil || *short.Pgn != *long.Pgn {
		t.Fatalf("pgn: short=%v long=%v", short.Pgn, long.Pgn)
	}
	_ = os.TempDir()
}

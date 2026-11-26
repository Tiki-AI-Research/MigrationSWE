package constants

import (
	"os"
	"path/filepath"
	"testing"
)

// configPath mirrors the executable's config resolution: CONFIG_DIR/chess-tui/config.toml.
func configPath(t *testing.T) string {
	t.Helper()
	dir, err := ConfigDir()
	if err != nil {
		t.Fatalf("ConfigDir: %v", err)
	}
	return filepath.Join(dir, "chess-tui", "config.toml")
}

// F2P-027 (R038): On Linux the config directory follows XDG semantics:
// XDG_CONFIG_HOME=/tmp/x resolves to /tmp/x/chess-tui/config.toml, and with
// XDG_CONFIG_HOME unset it is $HOME/.config/chess-tui/config.toml.
func TestF2P027ConfigDirXDG(t *testing.T) {
	prevXDG, hadXDG := os.LookupEnv("XDG_CONFIG_HOME")
	prevHome := os.Getenv("HOME")
	t.Cleanup(func() {
		if hadXDG {
			os.Setenv("XDG_CONFIG_HOME", prevXDG)
		} else {
			os.Unsetenv("XDG_CONFIG_HOME")
		}
		os.Setenv("HOME", prevHome)
	})

	os.Setenv("XDG_CONFIG_HOME", "/tmp/x")
	if got := configPath(t); got != "/tmp/x/chess-tui/config.toml" {
		t.Fatalf("XDG_CONFIG_HOME path = %q", got)
	}

	os.Unsetenv("XDG_CONFIG_HOME")
	os.Setenv("HOME", "/home/f2p")
	if got := configPath(t); got != "/home/f2p/.config/chess-tui/config.toml" {
		t.Fatalf("HOME fallback path = %q", got)
	}
}

// F2P-034 (R038): the Lichess endpoint and User-Agent constants are preserved
// (bearer authentication is asserted on the live request path in the lichess
// probe), and the config path keeps the XDG contract.
func TestF2P034LichessConstantsAndConfigPath(t *testing.T) {
	if LichessAPIURL != "https://lichess.org/api" {
		t.Fatalf("base url = %q", LichessAPIURL)
	}
	if LichessUserAgent != "chess-tui (https://github.com/thomas-mauran/chess-tui)" {
		t.Fatalf("user agent = %q", LichessUserAgent)
	}

	prevXDG, hadXDG := os.LookupEnv("XDG_CONFIG_HOME")
	t.Cleanup(func() {
		if hadXDG {
			os.Setenv("XDG_CONFIG_HOME", prevXDG)
		} else {
			os.Unsetenv("XDG_CONFIG_HOME")
		}
	})
	os.Setenv("XDG_CONFIG_HOME", "/tmp/f2p034")
	if got := configPath(t); got != "/tmp/f2p034/chess-tui/config.toml" {
		t.Fatalf("config path = %q", got)
	}
}

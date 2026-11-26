package preferences

// Probe tests transplanted into the reference target's `internal/preferences`
// package. They observe the on-disk `.preferences` contract: an empty value
// when missing or invalid, and an owner-only (0600) file on save.

import (
	"os"
	"path/filepath"
	"testing"
)

func f2pPreferencesPath(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("FT_DATA_DIR", dir)
	return filepath.Join(dir, ".preferences")
}

func f2pAssertPreferencesContract(t *testing.T) {
	t.Helper()
	path := f2pPreferencesPath(t)

	// Missing file -> empty value.
	if got := LoadPreferences(); got.DefaultEngine != "" {
		t.Fatalf("missing preferences should be empty, got %+v", got)
	}

	// Invalid file -> empty value.
	if err := os.WriteFile(path, []byte("{ not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := LoadPreferences(); got.DefaultEngine != "" {
		t.Fatalf("invalid preferences should be empty, got %+v", got)
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}

	// Save creates the file owner-only 0600.
	if err := SavePreferences(Preferences{DefaultEngine: "claude"}); err != nil {
		t.Fatalf("save: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("preferences mode = %04o, want 0600", perm)
	}
	if got := LoadPreferences(); got.DefaultEngine != "claude" {
		t.Fatalf("round-trip failed, got %+v", got)
	}
}

func TestF2P054PreferencesContract(t *testing.T) { f2pAssertPreferencesContract(t) }
func TestF2P065PreferencesContract(t *testing.T) { f2pAssertPreferencesContract(t) }

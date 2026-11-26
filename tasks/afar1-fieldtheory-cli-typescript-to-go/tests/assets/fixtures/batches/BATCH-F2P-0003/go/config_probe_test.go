package config

// Probe tests transplanted into the reference target's `internal/config`
// package. They invoke the production environment loader and observe the
// resulting process environment: values already present must not be
// overridden, and data-dir files must still be merged.

import (
	"os"
	"path/filepath"
	"testing"
)

func f2pWithEnv(t *testing.T) (cwd string, dataDir string) {
	t.Helper()
	base := t.TempDir()
	cwd = filepath.Join(base, "cwd")
	dataDir = filepath.Join(base, "data")
	if err := os.MkdirAll(cwd, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		t.Fatal(err)
	}
	// cwd .env sets K=v1
	if err := os.WriteFile(filepath.Join(cwd, ".env"), []byte("K=v1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// data-dir .env.local sets J=v2
	if err := os.WriteFile(filepath.Join(dataDir, ".env.local"), []byte("J=v2\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FT_DATA_DIR", dataDir)
	old, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(cwd); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chdir(old) })
	return cwd, dataDir
}

func f2pAssertNoOverrideAndMerge(t *testing.T) {
	t.Helper()
	t.Setenv("K", "v0")
	os.Unsetenv("J")
	f2pWithEnv(t)
	LoadEnv()
	if got := os.Getenv("K"); got != "v0" {
		t.Fatalf("existing env var K overridden: got %q, want %q", got, "v0")
	}
	if got := os.Getenv("J"); got != "v2" {
		t.Fatalf("data-dir .env.local not merged: J=%q, want %q", got, "v2")
	}
}

func TestF2P066EnvLoadingRuntimeEntry(t *testing.T) { f2pAssertNoOverrideAndMerge(t) }
func TestF2P067EnvLoadingDependencyExecution(t *testing.T) { f2pAssertNoOverrideAndMerge(t) }
func TestF2P072EnvLoadingRuntimeEntry(t *testing.T) { f2pAssertNoOverrideAndMerge(t) }

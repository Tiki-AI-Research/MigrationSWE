package fsutil

// F2P target-side probe overlay for BATCH-F2P-0001.
//
// Copied into internal/fsutil/ of a writable copy of the Go target and executed
// with `go test`. It asserts the documented write modes (JSON/JSONL 0600,
// Markdown 0644) and that reading a missing or malformed JSONL file yields an
// empty list.

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

// F2P-013: durable JSON/JSONL writes at 0600, Markdown at 0644, and empty list
// on missing/malformed JSONL.
func TestF2P013FilesystemWriteModes(t *testing.T) {
	old := syscall.Umask(0o022)
	defer syscall.Umask(old)

	dir := t.TempDir()

	jsonPath := filepath.Join(dir, "prefs.json")
	if err := WriteJSON(jsonPath, map[string]any{"a": 1}); err != nil {
		t.Fatalf("WriteJSON: %v", err)
	}
	if mode := f2pMode(t, jsonPath); mode != 0o600 {
		t.Fatalf("expected JSON write mode 0600, got %o", mode)
	}

	linesPath := filepath.Join(dir, "rows.jsonl")
	if err := WriteJSONLines(linesPath, []any{map[string]any{"id": "1"}}); err != nil {
		t.Fatalf("WriteJSONLines: %v", err)
	}
	if mode := f2pMode(t, linesPath); mode != 0o600 {
		t.Fatalf("expected JSONL write mode 0600, got %o", mode)
	}

	mdPath := filepath.Join(dir, "note.md")
	if err := WriteMd(mdPath, "# hi\n"); err != nil {
		t.Fatalf("WriteMd: %v", err)
	}
	if mode := f2pMode(t, mdPath); mode != 0o644 {
		t.Fatalf("expected Markdown write mode 0644, got %o", mode)
	}

	if rows := ReadJSONLines[map[string]any](filepath.Join(dir, "missing.jsonl")); len(rows) != 0 {
		t.Fatalf("expected empty list for missing JSONL, got %d", len(rows))
	}

	badPath := filepath.Join(dir, "bad.jsonl")
	if err := os.WriteFile(badPath, []byte("{ this is not json\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if rows := ReadJSONLines[map[string]any](badPath); len(rows) != 0 {
		t.Fatalf("expected empty list for malformed JSONL, got %d", len(rows))
	}
}

func f2pMode(t *testing.T, path string) os.FileMode {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat %s: %v", path, err)
	}
	return info.Mode().Perm()
}

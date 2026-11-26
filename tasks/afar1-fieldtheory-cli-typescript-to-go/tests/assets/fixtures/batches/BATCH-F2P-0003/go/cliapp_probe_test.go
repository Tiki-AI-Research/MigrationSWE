package cliapp

// Probe test transplanted into the reference target's `internal/cliapp`
// package. It exercises the LLM classification bridge (the Go replacement for
// bookmark-classify-llm.ts) end to end against a bounded fake engine, proving
// the engine invocation path is the production bridge.

import (
	"os"
	"path/filepath"
	"testing"

	"fieldtheory/internal/engine"
	"fieldtheory/internal/store"
)

func TestF2P060LlmBridgeInvocation(t *testing.T) {
	dataDir := t.TempDir()
	t.Setenv("FT_DATA_DIR", dataDir)
	line := `{"id":"b1","tweetId":"1","url":"https://x.com/a/status/1","text":"a handy cli tool","authorHandle":"alice","syncedAt":"2024-01-01T00:00:00.000Z"}` + "\n"
	if err := os.WriteFile(filepath.Join(dataDir, "bookmarks.jsonl"), []byte(line), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := store.BuildIndex(false); err != nil {
		t.Fatalf("build index: %v", err)
	}

	binDir := t.TempDir()
	script := "#!/bin/sh\n" + `cat >/dev/null
printf '[{"id":"b1","categories":["tool"],"primary":"tool"}]'` + "\n"
	if err := os.WriteFile(filepath.Join(binDir, "claude"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	e := engine.ResolvedEngine{Name: "claude", Config: engine.KnownEngines["claude"], Label: "claude"}
	res, err := classifyWithLlm(e, nil)
	if err != nil {
		t.Fatalf("classifyWithLlm: %v", err)
	}
	if res.TotalUnclassified != 1 || res.Classified != 1 {
		t.Fatalf("unexpected bridge result: %+v", res)
	}
}

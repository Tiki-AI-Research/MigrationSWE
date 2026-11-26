package engine

// F2P target-side probe overlay for BATCH-F2P-0001.
//
// This file is copied into internal/engine/ of a writable copy of the Go target
// and executed with `go test`. It only uses the exported engine API and asserts
// observable behavior (constructed argv, engine label, detection result and the
// structured EngineInvocationError). It deliberately reuses distinct helper
// names so it can coexist with the repository's own engine_test.go.

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func f2pEngineScript(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "engine")
	if err := os.WriteFile(p, []byte("#!/bin/sh\n"+body+"\n"), 0o755); err != nil {
		t.Fatalf("write script: %v", err)
	}
	return p
}

func f2pResolved(bin string) ResolvedEngine {
	return ResolvedEngine{
		Name: "fake",
		Config: EngineConfig{
			Bin: bin,
			Args: func(p string, model, effort string) []string {
				return []string{p}
			},
		},
		Label: "fake",
	}
}

// F2P-004: engine argv construction and label for claude and codex.
func TestF2P004EngineArgvAndLabel(t *testing.T) {
	claude := KnownEngines["claude"]
	got := claude.Args("PROMPT", "m", "e")
	want := []string{"-p", "--output-format", "text", "--model", "m", "--effort", "e", "PROMPT"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("claude argv mismatch:\n got %q\nwant %q", got, want)
	}

	codex := KnownEngines["codex"]
	got = codex.Args("PROMPT", "m", "e")
	want = []string{"exec", "--skip-git-repo-check", "--model", "m", "--config", `model_reasoning_effort="e"`, "PROMPT"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("codex argv mismatch:\n got %q\nwant %q", got, want)
	}

	if label := DescribeEngine("claude", "m", "e"); label != "claude/m/effort=e" {
		t.Fatalf("label mismatch: got %q want %q", label, "claude/m/effort=e")
	}
	if label := DescribeEngine("codex", "", ""); label != "codex" {
		t.Fatalf("bare label mismatch: got %q want %q", label, "codex")
	}
}

// F2P-004: engine discovery honors an executable file on PATH and rejects a
// non-executable file of the same name.
func TestF2P004EngineDiscoveryExecutableBit(t *testing.T) {
	dir := t.TempDir()
	execPath := filepath.Join(dir, "claude")
	if err := os.WriteFile(execPath, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	env := map[string]string{"PATH": dir}
	if !HasCommandOnPath("claude", env, "linux") {
		t.Fatalf("expected executable claude on PATH to be detected")
	}

	nonExecDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(nonExecDir, "codex"), []byte("not executable"), 0o644); err != nil {
		t.Fatal(err)
	}
	env = map[string]string{"PATH": nonExecDir}
	if HasCommandOnPath("codex", env, "linux") {
		t.Fatalf("expected non-executable codex to be rejected")
	}
}

// F2P-012: the invocation bridge closes stdin, trims stdout and maps failures
// to a structured EngineInvocationError.
func TestF2P012InvocationBridge(t *testing.T) {
	t.Run("closes-stdin-and-trims", func(t *testing.T) {
		bin := f2pEngineScript(t, `cat >/dev/null
printf '  hi  '`)
		e := f2pResolved(bin)
		done := make(chan struct{})
		var out string
		var err error
		go func() {
			out, err = InvokeEngine(e, "prompt", InvokeOptions{TimeoutMS: 5000})
			close(done)
		}()
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			t.Fatal("InvokeEngine blocked: stdin was not closed")
		}
		if err != nil || out != "hi" {
			t.Fatalf("expected trimmed %q with no error, got %q err=%v", "hi", out, err)
		}
	})

	t.Run("timeout", func(t *testing.T) {
		bin := f2pEngineScript(t, `sleep 5`)
		e := f2pResolved(bin)
		start := time.Now()
		_, err := InvokeEngine(e, "prompt", InvokeOptions{TimeoutMS: 200})
		if elapsed := time.Since(start); elapsed > 4*time.Second {
			t.Fatalf("timeout not enforced, took %s", elapsed)
		}
		var ie *EngineInvocationError
		if !errors.As(err, &ie) {
			t.Fatalf("expected EngineInvocationError, got %v", err)
		}
		if ie.Reason != ReasonTimeout || !ie.Killed {
			t.Fatalf("expected reason=%q killed=true, got reason=%q killed=%v", ReasonTimeout, ie.Reason, ie.Killed)
		}
	})

	t.Run("maxbuffer", func(t *testing.T) {
		bin := f2pEngineScript(t, `head -c 200000 /dev/zero | tr '\0' 'a'`)
		e := f2pResolved(bin)
		_, err := InvokeEngine(e, "prompt", InvokeOptions{TimeoutMS: 5000, MaxBuffer: 1024})
		var ie *EngineInvocationError
		if !errors.As(err, &ie) {
			t.Fatalf("expected EngineInvocationError, got %v", err)
		}
		if ie.Reason != ReasonMaxBuf {
			t.Fatalf("expected reason=%q, got %q", ReasonMaxBuf, ie.Reason)
		}
	})

	t.Run("exit-and-redact", func(t *testing.T) {
		bin := f2pEngineScript(t, `echo "token sk-ABCDEFGHIJKLMNOPQRSTUVWX" >&2
exit 3`)
		e := f2pResolved(bin)
		_, err := InvokeEngine(e, "prompt", InvokeOptions{TimeoutMS: 5000})
		var ie *EngineInvocationError
		if !errors.As(err, &ie) {
			t.Fatalf("expected EngineInvocationError, got %v", err)
		}
		if ie.Reason != ReasonExit {
			t.Fatalf("expected reason=%q, got %q", ReasonExit, ie.Reason)
		}
		if ie.Code == nil || *ie.Code != 3 {
			t.Fatalf("expected exit code 3, got %v", ie.Code)
		}
		if strings.Contains(ie.Stderr, "sk-ABCDEFGHIJKLMNOPQRSTUVWX") {
			t.Fatalf("provider key was not redacted: %q", ie.Stderr)
		}
	})
}

// F2P-015: explicit engine overrides fail fast; unknown engines are rejected
// before any invocation happens.
func TestF2P015ResolutionPrecedence(t *testing.T) {
	if _, err := ResolveEngine(EngineRunProfile{Engine: "bogus"}); err == nil {
		t.Fatalf("expected unknown engine to be rejected")
	} else if !strings.Contains(err.Error(), "bogus") {
		t.Fatalf("expected error to name the unknown engine, got %v", err)
	}

	t.Setenv("PATH", t.TempDir())
	if _, err := ResolveEngine(EngineRunProfile{Engine: "claude"}); err == nil {
		t.Fatalf("expected a known engine absent from PATH to be rejected")
	}
}

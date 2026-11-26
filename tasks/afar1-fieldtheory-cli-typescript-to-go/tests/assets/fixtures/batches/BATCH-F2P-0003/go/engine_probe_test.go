package engine

// Probe tests transplanted into the reference target's `internal/engine`
// package at probe time. They observe the engine bridge behavior only:
// structured error reasons, argv shape, label shape and redaction.

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func f2pWriteScript(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "fake-engine")
	if err := os.WriteFile(p, []byte("#!/bin/sh\n"+body+"\n"), 0o755); err != nil {
		t.Fatalf("write script: %v", err)
	}
	return p
}

func f2pResolved(bin string) ResolvedEngine {
	return ResolvedEngine{
		Name: "fake",
		Config: EngineConfig{
			Bin:  bin,
			Args: func(p, model, effort string) []string { return []string{p} },
		},
		Label: "fake",
	}
}

func TestF2P049EngineArgvAndLabel(t *testing.T) {
	claude := ResolvedEngine{
		Name: "claude", Config: KnownEngines["claude"],
		Model: "m", Effort: "e", Label: DescribeEngine("claude", "m", "e"),
	}
	wantClaude := []string{"-p", "--output-format", "text", "--model", "m", "--effort", "e", "<prompt>"}
	if got := claude.Args("<prompt>"); !reflect.DeepEqual(got, wantClaude) {
		t.Fatalf("claude argv = %#v, want %#v", got, wantClaude)
	}
	if claude.Label != "claude/m/effort=e" {
		t.Fatalf("claude label = %q, want %q", claude.Label, "claude/m/effort=e")
	}

	codex := ResolvedEngine{
		Name: "codex", Config: KnownEngines["codex"],
		Model: "m", Effort: "e", Label: DescribeEngine("codex", "m", "e"),
	}
	wantCodex := []string{"exec", "--skip-git-repo-check", "--model", "m", "--config", `model_reasoning_effort="e"`, "<prompt>"}
	if got := codex.Args("<prompt>"); !reflect.DeepEqual(got, wantCodex) {
		t.Fatalf("codex argv = %#v, want %#v", got, wantCodex)
	}

	if bare := DescribeEngine("claude", "", ""); bare != "claude" {
		t.Fatalf("bare label = %q, want %q", bare, "claude")
	}
}

func TestF2P055EngineInvocationBridge(t *testing.T) {
	sentinel := "PROMPT-SENTINEL-12345"

	// stdin closed immediately + trimmed stdout on exit 0.
	okBin := f2pWriteScript(t, `cat >/dev/null
printf '  hi  '`)
	out, err := InvokeEngine(f2pResolved(okBin), sentinel, InvokeOptions{TimeoutMS: 5000})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out != "hi" {
		t.Fatalf("trimmed stdout = %q, want %q", out, "hi")
	}

	// timeout -> reason=timeout, killed=true, prompt not inlined.
	stallBin := f2pWriteScript(t, `sleep 5`)
	_, err = InvokeEngine(f2pResolved(stallBin), sentinel, InvokeOptions{TimeoutMS: 200})
	var ie *EngineInvocationError
	if !errors.As(err, &ie) {
		t.Fatalf("expected EngineInvocationError, got %v", err)
	}
	if ie.Reason != ReasonTimeout || !ie.Killed {
		t.Fatalf("timeout: reason=%q killed=%v", ie.Reason, ie.Killed)
	}
	if strings.Contains(ie.Message, sentinel) {
		t.Fatalf("failure message inlined the prompt")
	}

	// buffer cap -> reason=maxbuffer.
	bigBin := f2pWriteScript(t, `i=0; while [ $i -lt 400 ]; do printf 'abcdefghij'; i=$((i+1)); done`)
	_, err = InvokeEngine(f2pResolved(bigBin), sentinel, InvokeOptions{TimeoutMS: 5000, MaxBuffer: 64})
	if !errors.As(err, &ie) || ie.Reason != ReasonMaxBuf {
		t.Fatalf("maxbuffer: err=%v", err)
	}

	// non-zero exit -> reason=exit with code, stderr redacted.
	secretBin := f2pWriteScript(t, `echo sk-ABCDEFGHIJKLMNOPQRSTUVWX >&2
exit 3`)
	_, err = InvokeEngine(f2pResolved(secretBin), sentinel, InvokeOptions{TimeoutMS: 5000})
	if !errors.As(err, &ie) {
		t.Fatalf("expected exit EngineInvocationError, got %v", err)
	}
	if ie.Reason != ReasonExit {
		t.Fatalf("exit: reason=%q, want %q", ie.Reason, ReasonExit)
	}
	if ie.Code == nil || *ie.Code != 3 {
		t.Fatalf("exit code = %v, want 3", ie.Code)
	}
	if strings.Contains(ie.Stderr, "sk-ABCDEFGHIJKLMNOPQRSTUVWX") {
		t.Fatalf("secret leaked in stderr: %q", ie.Stderr)
	}
	if strings.Contains(ie.Message, sentinel) {
		t.Fatalf("failure message inlined the prompt")
	}

	// spawn failure -> reason=spawn.
	_, err = InvokeEngine(f2pResolved(filepath.Join(t.TempDir(), "nope")), sentinel, InvokeOptions{TimeoutMS: 2000})
	if !errors.As(err, &ie) || ie.Reason != ReasonSpawn {
		t.Fatalf("spawn: err=%v", err)
	}
}

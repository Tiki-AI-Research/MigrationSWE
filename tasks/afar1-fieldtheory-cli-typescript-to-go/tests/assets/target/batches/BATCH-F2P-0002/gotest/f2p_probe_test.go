package f2pprobe

import (
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"fieldtheory/internal/engine"
	"fieldtheory/internal/prompt"
)

func TestF2PEngineArgvAndLabel(t *testing.T) {
	claude := engine.KnownEngines["claude"].Args("PROMPT", "m", "e")
	wantClaude := []string{"-p", "--output-format", "text", "--model", "m", "--effort", "e", "PROMPT"}
	if strings.Join(claude, "\x00") != strings.Join(wantClaude, "\x00") {
		t.Fatalf("claude argv=%q want %q", claude, wantClaude)
	}
	codex := engine.KnownEngines["codex"].Args("PROMPT", "m", "e")
	wantCodex := []string{"exec", "--skip-git-repo-check", "--model", "m", "--config", `model_reasoning_effort="e"`, "PROMPT"}
	if strings.Join(codex, "\x00") != strings.Join(wantCodex, "\x00") {
		t.Fatalf("codex argv=%q want %q", codex, wantCodex)
	}
	if got := engine.DescribeEngine("claude", "m", "e"); got != "claude/m/effort=e" {
		t.Fatalf("claude label=%q", got)
	}
	if got := engine.DescribeEngine("codex", "m", "e"); got != "codex/m/effort=e" {
		t.Fatalf("codex label=%q", got)
	}
}

func stubEngine(t *testing.T, dir, name, body string) engine.ResolvedEngine {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	return engine.ResolvedEngine{Name: name, Config: engine.EngineConfig{
		Bin:  p,
		Args: func(string, string, string) []string { return nil },
	}}
}

func TestF2PEngineInvocation(t *testing.T) {
	dir := t.TempDir()

	trim := stubEngine(t, dir, "trim", "#!/bin/sh\ncat >/dev/null\nprintf '  hi  '\n")
	out, err := engine.InvokeEngine(trim, "PROMPT", engine.InvokeOptions{TimeoutMS: 5000})
	if err != nil || out != "hi" {
		t.Fatalf("trim stdout=%q err=%v", out, err)
	}

	sleepy := stubEngine(t, dir, "sleepy", "#!/bin/sh\ncat >/dev/null\nsleep 5\n")
	start := time.Now()
	_, err = engine.InvokeEngine(sleepy, "PROMPT", engine.InvokeOptions{TimeoutMS: 300})
	if time.Since(start) > 4*time.Second {
		t.Fatal("timeout did not terminate the child promptly")
	}
	ie, ok := err.(*engine.EngineInvocationError)
	if !ok || ie.Reason != engine.ReasonTimeout || !ie.Killed {
		t.Fatalf("timeout error=%#v", err)
	}

	big := stubEngine(t, dir, "big", "#!/bin/sh\ncat >/dev/null\nhead -c 200000 /dev/zero\n")
	_, err = engine.InvokeEngine(big, "PROMPT", engine.InvokeOptions{TimeoutMS: 5000, MaxBuffer: 1024})
	ie, ok = err.(*engine.EngineInvocationError)
	if !ok || ie.Reason != engine.ReasonMaxBuf {
		t.Fatalf("maxbuffer error=%#v", err)
	}

	exit3 := stubEngine(t, dir, "exit3", "#!/bin/sh\ncat >/dev/null\nprintf 'sk-ABCDEFGHIJKLMNOPQRSTUVWX\\n' >&2\nexit 3\n")
	_, err = engine.InvokeEngine(exit3, "PROMPT", engine.InvokeOptions{TimeoutMS: 5000})
	ie, ok = err.(*engine.EngineInvocationError)
	if !ok || ie.Reason != engine.ReasonExit || ie.Code == nil || *ie.Code != 3 {
		t.Fatalf("exit error=%#v", err)
	}
	if strings.Contains(ie.Stderr, "ABCDEFGHIJKLMNOPQRSTUVWX") || !strings.Contains(ie.Stderr, "REDACTED") {
		t.Fatalf("secret not redacted: %q", ie.Stderr)
	}
}

func TestF2PPromptCloseOnEOF(t *testing.T) {
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	old := os.Stdin
	os.Stdin = r
	defer func() { os.Stdin = old; _ = r.Close(); _ = w.Close() }()
	_ = w.Close()
	res := prompt.PromptText("q")
	if res.Kind != prompt.KindClose {
		t.Fatalf("kind=%v want close", res.Kind)
	}
}

func TestF2PPromptInterruptOnSIGINT(t *testing.T) {
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	old := os.Stdin
	os.Stdin = r
	defer func() { os.Stdin = old; _ = r.Close(); _ = w.Close() }()

	done := make(chan prompt.Result, 1)
	go func() { done <- prompt.PromptText("q") }()
	time.Sleep(300 * time.Millisecond)
	_ = syscall.Kill(os.Getpid(), syscall.SIGINT)

	select {
	case res := <-done:
		if res.Kind != prompt.KindInterrupt {
			t.Fatalf("kind=%v want interrupt", res.Kind)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("prompt did not resolve on SIGINT")
	}
}

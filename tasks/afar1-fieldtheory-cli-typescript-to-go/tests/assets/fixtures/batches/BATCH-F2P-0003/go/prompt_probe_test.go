package prompt

// Probe tests transplanted into the reference target's `internal/prompt`
// package. They drive the interactive helper through a controlled stdin and
// observe the Result kind: answer (trimmed), close (EOF without an answer) and
// interrupt (SIGINT).

import (
	"os"
	"syscall"
	"testing"
	"time"
)

func TestF2P070PromptAnswerCloseInterrupt(t *testing.T) {
	origStdin := os.Stdin
	defer func() { os.Stdin = origStdin }()

	// Trimmed answer.
	ar, aw, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdin = ar
	if _, err := aw.WriteString("  hello  \n"); err != nil {
		t.Fatal(err)
	}
	aw.Close()
	res := PromptText("")
	ar.Close()
	if res.Kind != KindAnswer || res.Value != "hello" {
		t.Fatalf("answer: got %+v, want kind=answer value=hello", res)
	}

	// Close: EOF with no answer.
	cr, cw, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdin = cr
	cw.Close()
	res = PromptText("")
	cr.Close()
	if res.Kind != KindClose {
		t.Fatalf("close: got %+v, want kind=close", res)
	}

	// Interrupt: SIGINT while blocked.
	ir, iw, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdin = ir
	done := make(chan Result, 1)
	go func() { done <- PromptText("") }()
	time.Sleep(50 * time.Millisecond)
	if err := syscall.Kill(os.Getpid(), syscall.SIGINT); err != nil {
		t.Fatal(err)
	}
	select {
	case res = <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("PromptText did not return after SIGINT")
	}
	ir.Close()
	iw.Close()
	if res.Kind != KindInterrupt {
		t.Fatalf("interrupt: got %+v, want kind=interrupt", res)
	}
}

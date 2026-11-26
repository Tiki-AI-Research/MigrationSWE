package main

// Contract probe for observation CONTRACT-019 (requirement R022).
//
// The targeted implementation must report the exit code of an
// `aws ssm start-session` subprocess, and exit code 254 must additionally
// indicate that the instance is not connected to the SSM Agent.
//
// The probe is copied next to the migrated main package and executed with:
//   timeout 300 go test -count=1 -run 'TestReportSSMExit' -v .
// It only inspects process-observable behavior (printed lines) and the
// deterministic 254 special case.

import (
	"io"
	"os"
	"strings"
	"testing"
)

func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	old := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	os.Stdout = w
	done := make(chan string, 1)
	go func() {
		data, _ := io.ReadAll(r)
		done <- string(data)
	}()
	fn()
	_ = w.Close()
	os.Stdout = old
	return <-done
}

func TestReportSSMExitReports254Hint(t *testing.T) {
	out := captureStdout(t, func() { reportSSMExit(254) })
	if !strings.Contains(out, "SSM session exited with code: 254") {
		t.Fatalf("missing exit-code line for 254, got %q", out)
	}
	if !strings.Contains(out, "not connected to SSM") {
		t.Fatalf("missing SSM Agent hint for 254, got %q", out)
	}
}

func TestReportSSMExitReportsOtherCodes(t *testing.T) {
	out := captureStdout(t, func() { reportSSMExit(1) })
	if !strings.Contains(out, "SSM session exited with code: 1") {
		t.Fatalf("missing exit-code line for 1, got %q", out)
	}
	if strings.Contains(out, "not connected to SSM") {
		t.Fatalf("254 hint must not appear for exit code 1, got %q", out)
	}
}

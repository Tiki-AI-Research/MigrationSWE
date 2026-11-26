package main

import (
	"io"
	"os"
	"strings"
	"testing"
)

func captureReportSSMExit(t *testing.T, code int) string {
	t.Helper()
	old := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = w
	reportSSMExit(code)
	_ = w.Close()
	os.Stdout = old
	data, err := io.ReadAll(r)
	if err != nil {
		t.Fatal(err)
	}
	_ = r.Close()
	return string(data)
}

func TestDiff019SSMExitReporting(t *testing.T) {
	out254 := captureReportSSMExit(t, 254)
	if !strings.Contains(out254, "SSM session exited with code: 254") {
		t.Fatalf("254 output = %q", out254)
	}
	lower := strings.ToLower(out254)
	if !strings.Contains(lower, "not connected") {
		t.Fatalf("254 output should mention the instance is not connected: %q", out254)
	}

	out1 := captureReportSSMExit(t, 1)
	if !strings.Contains(out1, "SSM session exited with code: 1") {
		t.Fatalf("1 output = %q", out1)
	}
	if strings.Contains(strings.ToLower(out1), "not connected") {
		t.Fatalf("non-254 output must not include the 254 hint: %q", out1)
	}
}

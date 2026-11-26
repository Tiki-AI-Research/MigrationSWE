package surface

import (
	"os"
	"path/filepath"
	"testing"
)

func TestExportedNames(t *testing.T) {
	for _, scenario := range []struct {
		name  string
		body  string
		extra string
		want  bool
	}{
		{name: "function", body: "func Labeler(options ...any) error { return nil }", want: true},
		{name: "type", body: "type Labeler struct{}", want: true},
		{name: "alias", body: "type Labeler = struct{}", want: true},
		{name: "function-value", body: "var Labeler = func() error { return nil }", want: true},
		{name: "missing"},
		{name: "comment", body: "// type Labeler struct{}"},
		{name: "method", body: "type Other struct{}\nfunc (Other) Labeler() {}"},
		{name: "test-only", extra: "api_test.go"},
		{name: "excluded", extra: "excluded.go"},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			dir := t.TempDir()
			write := func(name, text string) {
				t.Helper()
				if err := os.WriteFile(filepath.Join(dir, name), []byte(text), 0600); err != nil {
					t.Fatal(err)
				}
			}
			write("api.go", "package labeler\n" + scenario.body + "\n")
			if scenario.extra != "" {
				prefix := ""
				if scenario.extra == "excluded.go" {
					prefix = "//go:build labeler_contract_disabled && !labeler_contract_disabled\n\n"
				}
				write(scenario.extra, prefix+"package labeler\ntype Labeler struct{}\n")
			}
			found, err := exportedNames(dir)
			if err != nil {
				t.Fatal(err)
			}
			if found["Labeler"] != scenario.want {
				t.Errorf("Labeler present = %t, want %t", found["Labeler"], scenario.want)
			}
		})
	}
}

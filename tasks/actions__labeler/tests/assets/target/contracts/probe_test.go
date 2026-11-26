// Package contracts checks only the public surface named by instruction.md.
// Behaviour is exercised separately through the real action entrypoint.
package contracts

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

func root(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("go.mod not found")
		}
		dir = parent
	}
}

func actionSection(t *testing.T, name string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(root(t), "action.yml"))
	if err != nil {
		t.Fatal(err)
	}
	var action map[string]any
	if err := yaml.Unmarshal(data, &action); err != nil {
		t.Fatal(err)
	}
	section, ok := action[name].(map[string]any)
	if !ok {
		t.Fatalf("action.yml %s must be a mapping", name)
	}
	return section
}

func TestActionInputs(t *testing.T) {
	inputs := actionSection(t, "inputs")
	for name, want := range map[string]string{
		"repo-token": "${{ github.token }}", "configuration-path": ".github/labeler.yml",
		"sync-labels": "false", "dot": "true", "pr-number": "",
	} {
		input, ok := inputs[name].(map[string]any)
		if !ok {
			t.Errorf("missing input %q", name)
			continue
		}
		got := ""
		if value, exists := input["default"]; exists && value != nil {
			got = fmt.Sprint(value)
		}
		if got != want {
			t.Errorf("input %q default = %q, want %q", name, got, want)
		}
		if input["required"] == true || input["required"] == "true" {
			t.Errorf("input %q must remain optional", name)
		}
	}
}

func TestActionOutputs(t *testing.T) {
	outputs := actionSection(t, "outputs")
	runs := actionSection(t, "runs")
	for _, name := range []string{"new-labels", "all-labels"} {
		output, ok := outputs[name].(map[string]any)
		if !ok {
			t.Errorf("missing output %q", name)
			continue
		}
		if runs["using"] == "composite" {
			value, ok := output["value"].(string)
			if !ok || strings.TrimSpace(value) == "" {
				t.Errorf("composite action output %q must bind its value; writing a step's GITHUB_OUTPUT alone does not expose an action output", name)
			}
		}
	}
}


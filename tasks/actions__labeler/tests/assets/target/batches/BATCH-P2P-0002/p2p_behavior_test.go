// Package p2p holds the executable pass-to-pass projection for the labeler
// action migration (BATCH-P2P-0002).
//
// Every test drives the compiled Go action binary as a black box: it starts a
// local fake GitHub API, writes the event payload and label configuration to
// disk, runs the binary with the GitHub Actions runtime environment and then
// observes the process exit code, the GITHUB_OUTPUT file and the HTTP calls the
// action issued. No production symbol is imported, so the projection only
// depends on the public action contract.
package p2p

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

const buildTimeout = 300 * time.Second
const runTimeout = 60 * time.Second

var binaryPath string
var repoRoot string

func TestMain(m *testing.M) {
	root, err := findRepoRoot()
	if err != nil {
		fmt.Fprintf(os.Stderr, "cannot locate repository root: %v\n", err)
		os.Exit(1)
	}
	repoRoot = root
	bin := filepath.Join(os.TempDir(), fmt.Sprintf("labeler-p2p-%d", os.Getpid()))
	ctx, cancel := context.WithTimeout(context.Background(), buildTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "go", "build", "-o", bin, "./cmd/labeler")
	cmd.Dir = repoRoot
	if out, err := cmd.CombinedOutput(); err != nil {
		fmt.Fprintf(os.Stderr, "failed to build ./cmd/labeler: %v\n%s\n", err, out)
		os.Exit(1)
	}
	binaryPath = bin
	code := m.Run()
	_ = os.Remove(bin)
	os.Exit(code)
}

func findRepoRoot() (string, error) {
	dir, err := os.Getwd()
	if err != nil {
		return "", err
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", fmt.Errorf("go.mod not found above %s", dir)
		}
		dir = parent
	}
}

// ---------------------------------------------------------------------------
// Fake GitHub API
// ---------------------------------------------------------------------------

type label struct {
	Name   string `json:"name"`
	NodeID string `json:"node_id"`
}

type removal struct {
	LabelableID string
	LabelIDs    []string
}

type fakeGitHub struct {
	mu sync.Mutex

	prNumber   int
	prNodeID   string
	prLabels   []label
	changed    []string
	getPullErr bool
	head       string
	base       string
	noFiles    bool
	addStatus  int
	addMessage string
	addVerify  bool
	removeErr  bool

	added        [][]string
	removed      []removal
	verified     []label
	addCalls     int
	changedCalls int
}

func newFakeGitHub() *fakeGitHub {
	return &fakeGitHub{prNumber: 123, prNodeID: "PR_node"}
}

func (f *fakeGitHub) handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		path := strings.Trim(r.URL.Path, "/")
		parts := strings.Split(path, "/")

		switch {
		case path == "graphql":
			var body struct {
				Variables struct {
					LabelableID string   `json:"labelableId"`
					LabelIDs    []string `json:"labelIds"`
				} `json:"variables"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			f.removed = append(f.removed, removal{body.Variables.LabelableID, body.Variables.LabelIDs})
			if f.removeErr {
				writeJSON(w, map[string]interface{}{"errors": []map[string]string{{"message": "GraphQL request failed"}}})
				return
			}
			writeJSON(w, map[string]interface{}{"data": map[string]interface{}{}})

		case len(parts) == 5 && parts[3] == "pulls":
			if f.getPullErr {
				w.WriteHeader(http.StatusNotFound)
				writeJSON(w, map[string]string{"message": "Not Found"})
				return
			}
			labels := make([]map[string]string, 0, len(f.prLabels))
			for _, l := range f.prLabels {
				labels = append(labels, map[string]string{"name": l.Name, "node_id": l.NodeID})
			}
			writeJSON(w, map[string]interface{}{"number": f.prNumber, "node_id": f.prNodeID, "labels": labels,
				"head": map[string]string{"ref": f.head}, "base": map[string]string{"ref": f.base}})

		case len(parts) == 6 && parts[3] == "pulls" && parts[5] == "files":
			f.changedCalls++
			files := []map[string]string{}
			if !f.noFiles {
				for _, c := range f.changed {
					files = append(files, map[string]string{"filename": c})
				}
			}
			writeJSON(w, files)

		case len(parts) >= 5 && parts[3] == "contents":
			w.WriteHeader(http.StatusNotFound)
			writeJSON(w, map[string]string{"message": "Not Found"})

		case len(parts) == 6 && parts[3] == "issues" && parts[5] == "labels":
			if r.Method == http.MethodPost {
				var body struct {
					Labels []string `json:"labels"`
				}
				_ = json.NewDecoder(r.Body).Decode(&body)
				f.addCalls++
				f.added = append(f.added, body.Labels)
				if f.addStatus >= 400 {
					if f.addVerify {
						for _, l := range body.Labels {
							f.verified = append(f.verified, label{Name: l})
						}
						w.WriteHeader(f.addStatus)
						writeJSON(w, map[string]string{"message": f.addMessage})
						return
					}
					w.WriteHeader(f.addStatus)
					writeJSON(w, map[string]string{"message": f.addMessage})
					return
				}
				writeJSON(w, []map[string]string{})
				return
			}
			labels := make([]map[string]string, 0, len(f.prLabels)+len(f.verified))
			for _, l := range append(append([]label{}, f.prLabels...), f.verified...) {
				labels = append(labels, map[string]string{"name": l.Name})
			}
			writeJSON(w, labels)

		default:
			w.WriteHeader(http.StatusNotFound)
			writeJSON(w, map[string]string{"message": "Not Found"})
		}
	}
}

func writeJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

type actionOptions struct {
	event        string
	config       string
	configPath   string
	dot          *bool
	syncLabels   *bool
	prNumber     string
	extraEnv     []string
	noEvent      bool
	extraChanged []string
}

type actionResult struct {
	exitCode int
	stdout   string
	outputs  map[string]string
}

func runAction(t *testing.T, f *fakeGitHub, o actionOptions) actionResult {
	t.Helper()
	srv := httptest.NewServer(f.handler())
	t.Cleanup(srv.Close)

	dir := t.TempDir()
	eventPath := filepath.Join(dir, "event.json")
	event := o.event
	if event == "" {
		event = `{"pull_request":{"number":123,"node_id":"PR_node","labels":[],"head":{"ref":"main"},"base":{"ref":"main"}}}`
	}
	var payload struct {
		PullRequest struct {
			Head struct{ Ref string } `json:"head"`
			Base struct{ Ref string } `json:"base"`
		} `json:"pull_request"`
	}
	if err := json.Unmarshal([]byte(event), &payload); err != nil {
		t.Fatalf("parse fixture event: %v", err)
	}
	f.head, f.base = payload.PullRequest.Head.Ref, payload.PullRequest.Base.Ref
	if err := os.WriteFile(eventPath, []byte(event), 0o644); err != nil {
		t.Fatalf("write event: %v", err)
	}

	configPath := o.configPath
	if configPath == "" {
		configPath = filepath.Join(dir, "labeler.yml")
		if err := os.WriteFile(configPath, []byte(o.config), 0o644); err != nil {
			t.Fatalf("write config: %v", err)
		}
	}

	outPath := filepath.Join(dir, "github_output")
	if err := os.WriteFile(outPath, nil, 0o644); err != nil {
		t.Fatalf("create output command file: %v", err)
	}

	env := []string{
		"GITHUB_API_URL=" + srv.URL,
		"GITHUB_GRAPHQL_URL=" + srv.URL + "/graphql",
		"GITHUB_REPOSITORY=monalisa/helloworld",
		"GITHUB_SHA=deadbeef",
		"GITHUB_EVENT_NAME=pull_request",
		"GITHUB_ACTION_REPOSITORY=actions/labeler",
		"GITHUB_OUTPUT=" + outPath,
		"INPUT_REPO-TOKEN=token",
		"INPUT_CONFIGURATION-PATH=" + configPath,
	}
	if !o.noEvent {
		env = append(env, "GITHUB_EVENT_PATH="+eventPath)
	}
	dot, syncLabels := true, false
	if o.dot != nil { dot = *o.dot }
	if o.syncLabels != nil { syncLabels = *o.syncLabels }
	env = append(env, fmt.Sprintf("INPUT_DOT=%t", dot), fmt.Sprintf("INPUT_SYNC-LABELS=%t", syncLabels))
	if o.prNumber != "" {
		env = append(env, "INPUT_PR-NUMBER="+o.prNumber)
	}
	env = append(env, o.extraEnv...)

	ctx, cancel := context.WithTimeout(context.Background(), runTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, binaryPath)
	cmd.Dir = repoRoot
	cmd.Env = append(os.Environ(), env...)
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stdout
	err := cmd.Run()
	code := 0
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			code = ee.ExitCode()
		} else {
			t.Fatalf("run binary: %v", err)
		}
	}
	return actionResult{exitCode: code, stdout: stdout.String(), outputs: parseOutputs(t, outPath)}
}

func parseOutputs(t *testing.T, path string) map[string]string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		return map[string]string{}
	}
	lines := strings.Split(string(data), "\n")
	out := map[string]string{}
	for i := 0; i < len(lines); i++ {
		// GitHub Actions accepts both the multiline heredoc form
		// ``name<<DELIM`` and the single-line ``name=value`` form; both are
		// documented and carry the same observable value.
		if idx := strings.Index(lines[i], "<<"); idx >= 0 {
			name := lines[i][:idx]
			delim := lines[i][idx+2:]
			var val []string
			i++
			for i < len(lines) && lines[i] != delim {
				val = append(val, lines[i])
				i++
			}
			out[name] = strings.Join(val, "\n")
			continue
		}
		if idx := strings.Index(lines[i], "="); idx > 0 {
			out[lines[i][:idx]] = lines[i][idx+1:]
		}
	}
	return out
}

func boolPtr(b bool) *bool { return &b }

func splitCSV(s string) []string {
	if s == "" {
		return nil
	}
	return strings.Split(s, ",")
}

// ---------------------------------------------------------------------------
// P2P observations
// ---------------------------------------------------------------------------

// P2P-028: the end-to-end public capability of adding matching labels and
// publishing both outputs remains available.
func TestPublicCapabilitiesEndToEnd(t *testing.T) {
	f := newFakeGitHub()
	f.changed = []string{"foo.pdf"}
	cfg := "pdf:\n  - changed-files:\n      - any-glob-to-any-file: '*.pdf'\n"
	res := runAction(t, f, actionOptions{config: cfg})
	if res.exitCode != 0 {
		t.Fatalf("exit=%d stdout=%q", res.exitCode, res.stdout)
	}
	if res.outputs["new-labels"] != "pdf" || res.outputs["all-labels"] != "pdf" {
		t.Fatalf("outputs=%v", res.outputs)
	}
	if len(f.added) != 1 || strings.Join(f.added[0], ",") != "pdf" {
		t.Fatalf("added=%v", f.added)
	}
}

// P2P-027: action.yml input names/defaults are preserved.
func TestActionYMLInputsPreserved(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(repoRoot, "action.yml"))
	if err != nil {
		t.Fatalf("read action.yml: %v", err)
	}
	text := string(data)
	want := []string{
		"repo-token:", "configuration-path:", "sync-labels:", "dot:", "pr-number:",
	}
	for _, w := range want {
		if !strings.Contains(text, w) {
			t.Errorf("action.yml missing input %q", w)
		}
	}
	for _, def := range []string{"github.token", ".github/labeler.yml", "false", "true"} {
		if !strings.Contains(text, def) {
			t.Errorf("action.yml missing default %q", def)
		}
	}
}

// P2P-035: action.yml output names are preserved.
func TestActionYMLOutputsPreserved(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(repoRoot, "action.yml"))
	if err != nil {
		t.Fatalf("read action.yml: %v", err)
	}
	text := string(data)
	for _, w := range []string{"new-labels:", "all-labels:"} {
		if !strings.Contains(text, w) {
			t.Errorf("action.yml missing output %q", w)
		}
	}
}

// P2P-030: a decimal changed-files-labels-limit is rejected.
func TestInvalidDecimalLimitFails(t *testing.T) {
	f := newFakeGitHub()
	f.changed = []string{"foo.pdf"}
	cfg := "changed-files-labels-limit: 1.5\npdf:\n  - changed-files:\n      - any-glob-to-any-file: '*.pdf'\n"
	res := runAction(t, f, actionOptions{config: cfg})
	if res.exitCode == 0 {
		t.Fatalf("expected non-zero exit for decimal limit, stdout=%q", res.stdout)
	}
}

// P2P-034: multiple MatchConfigs supplied under any aggregate with OR.
func TestMultipleMatchConfigs(t *testing.T) {
	f := newFakeGitHub()
	f.changed = []string{"foo.pdf"}
	cfg := "multi:\n  - any:\n      - changed-files:\n          - any-glob-to-any-file: '*.pdf'\n      - head-branch: '^does-not-match$'\n"
	res := runAction(t, f, actionOptions{config: cfg})
	if res.exitCode != 0 {
		t.Fatalf("exit=%d stdout=%q", res.exitCode, res.stdout)
	}
	if got := res.outputs["new-labels"]; got != "multi" {
		t.Fatalf("new-labels=%q stdout=%q", got, res.stdout)
	}
}

// P2P-032: an empty changed-files array never matches.
func TestEmptyChangedFilesArray(t *testing.T) {
	f := newFakeGitHub()
	f.changed = []string{"foo.pdf"}
	cfg := "empty:\n  - changed-files: []\n"
	res := runAction(t, f, actionOptions{config: cfg})
	if res.exitCode != 0 {
		t.Fatalf("exit=%d stdout=%q", res.exitCode, res.stdout)
	}
	if got := res.outputs["new-labels"]; got != "" {
		t.Fatalf("expected no labels, got %q", got)
	}
}

// P2P-025: labels absent from the configuration are never removed and
// concurrently added labels remain in all-labels.
func TestUnconfiguredLabelsPreserved(t *testing.T) {
	f := newFakeGitHub()
	f.changed = []string{"foo.txt"}
	f.prLabels = []label{{Name: "external", NodeID: "n1"}, {Name: "concurrent", NodeID: "n2"}}
	cfg := "pdf:\n  - changed-files:\n      - any-glob-to-any-file: '*.pdf'\n"
	res := runAction(t, f, actionOptions{config: cfg, syncLabels: boolPtr(true)})
	if res.exitCode != 0 {
		t.Fatalf("exit=%d stdout=%q", res.exitCode, res.stdout)
	}
	if len(f.removed) != 0 {
		t.Fatalf("unexpected removals: %v", f.removed)
	}
	if got := res.outputs["all-labels"]; got != "external,concurrent" {
		t.Fatalf("all-labels=%q", got)
	}
}

// P2P-026: minimatch dot handling, negation and JavaScript regexp branch
// matching are preserved.
func TestGlobAndBranchSemantics(t *testing.T) {
	// dot=false: a glob must not match a dotfile.
	f := newFakeGitHub()
	f.changed = []string{".hidden/x.ts"}
	cfg := "ts:\n  - changed-files:\n      - any-glob-to-any-file: '**/*.ts'\n"
	res := runAction(t, f, actionOptions{config: cfg, dot: boolPtr(false)})
	if res.exitCode != 0 || res.outputs["new-labels"] != "" {
		t.Fatalf("dot=false: exit=%d out=%q", res.exitCode, res.outputs["new-labels"])
	}

	// dot=true: the same glob matches the dotfile.
	f2 := newFakeGitHub()
	f2.changed = []string{".hidden/x.ts"}
	res2 := runAction(t, f2, actionOptions{config: cfg, dot: boolPtr(true)})
	if res2.exitCode != 0 || res2.outputs["new-labels"] != "ts" {
		t.Fatalf("dot=true: exit=%d out=%q", res2.exitCode, res2.outputs["new-labels"])
	}

	// Negation: !docs/** matches src/a.ts but not docs/a.md.
	f3 := newFakeGitHub()
	f3.changed = []string{"src/a.ts"}
	neg := "non-docs:\n  - changed-files:\n      - any-glob-to-any-file: '!docs/**'\n"
	res3 := runAction(t, f3, actionOptions{config: neg})
	if res3.exitCode != 0 || res3.outputs["new-labels"] != "non-docs" {
		t.Fatalf("negation src: exit=%d out=%q", res3.exitCode, res3.outputs["new-labels"])
	}
	f4 := newFakeGitHub()
	f4.changed = []string{"docs/a.md"}
	res4 := runAction(t, f4, actionOptions{config: neg})
	if res4.outputs["new-labels"] != "" {
		t.Fatalf("negation docs: out=%q", res4.outputs["new-labels"])
	}

	// Branch pattern is a JavaScript regular expression.
	f5 := newFakeGitHub()
	f5.changed = []string{"foo.txt"}
	br := "feature:\n  - head-branch: '^feature/.*$'\n"
	res5 := runAction(t, f5, actionOptions{config: br, event: `{"pull_request":{"number":123,"node_id":"PR_node","labels":[],"head":{"ref":"feature/x"},"base":{"ref":"main"}}}`})
	if res5.exitCode != 0 || res5.outputs["new-labels"] != "feature" {
		t.Fatalf("branch match: exit=%d out=%q", res5.exitCode, res5.outputs["new-labels"])
	}
}

// P2P-029: a 5xx label addition is accepted when the labels were committed.
func TestAddLabelsServerErrorRecovery(t *testing.T) {
	f := newFakeGitHub()
	f.changed = []string{"foo.pdf"}
	f.addStatus = http.StatusInternalServerError
	f.addMessage = "boom"
	f.addVerify = true
	cfg := "pdf:\n  - changed-files:\n      - any-glob-to-any-file: '*.pdf'\n"
	res := runAction(t, f, actionOptions{config: cfg})
	if res.exitCode != 0 {
		t.Fatalf("expected recovery, exit=%d stdout=%q", res.exitCode, res.stdout)
	}
	if got := res.outputs["new-labels"]; got != "pdf" {
		t.Fatalf("new-labels=%q", got)
	}
}

// P2P-031: a failed bulk removal fails the step.
func TestBulkRemovalFailure(t *testing.T) {
	f := newFakeGitHub()
	f.changed = []string{"foo.txt"}
	f.prLabels = []label{{Name: "stale", NodeID: "stale_node"}}
	f.removeErr = true
	cfg := "stale:\n  - changed-files:\n      - any-glob-to-any-file: '*.pdf'\n"
	res := runAction(t, f, actionOptions{config: cfg, syncLabels: boolPtr(true)})
	if res.exitCode == 0 {
		t.Fatalf("expected non-zero exit, stdout=%q", res.stdout)
	}
}

// P2P-033: the documented configuration schema is accepted unchanged.
func TestFullConfigSchemaAccepted(t *testing.T) {
	f := newFakeGitHub()
	f.changed = []string{"components/a/file.ts", "docs/guide.md"}
	f.prLabels = []label{{Name: "docs", NodeID: "n_docs"}}
	cfg := "changed-files-labels-limit: 3\nmax-files-changed: 10\ndocs:\n  - all:\n      - changed-files:\n          - any-glob-to-any-file: 'docs/**'\n      - base-branch: '^main$'\ncomponent:\n  - changed-files:\n      - any-glob-to-any-file: 'components/**'\n"
	event := `{"pull_request":{"number":123,"node_id":"PR_node","labels":[{"name":"docs","node_id":"n_docs"}],"head":{"ref":"topic"},"base":{"ref":"main"}}}`
	res := runAction(t, f, actionOptions{config: cfg, event: event})
	if res.exitCode != 0 {
		t.Fatalf("exit=%d stdout=%q", res.exitCode, res.stdout)
	}
	if got := res.outputs["new-labels"]; !strings.Contains(got, "component") {
		t.Fatalf("new-labels=%q stdout=%q", got, res.stdout)
	}
}

// P2P-036: non-printable pr-number characters are escaped as \xNN.
func TestPRNumberEscape(t *testing.T) {
	f := newFakeGitHub()
	cfg := "pdf:\n  - changed-files:\n      - any-glob-to-any-file: '*.pdf'\n"
	res := runAction(t, f, actionOptions{config: cfg, prNumber: "12\x073"})
	if res.exitCode != 0 {
		t.Fatalf("exit=%d stdout=%q", res.exitCode, res.stdout)
	}
	if !strings.Contains(res.stdout, `\x07`) {
		t.Fatalf("expected escaped control byte in stdout, got %q", res.stdout)
	}
}

// P2P-037: a pull request with no changed files is skipped.
func TestNoChangedFilesSkipped(t *testing.T) {
	f := newFakeGitHub()
	f.noFiles = true
	cfg := "pdf:\n  - changed-files:\n      - any-glob-to-any-file: '*.pdf'\n"
	res := runAction(t, f, actionOptions{config: cfg})
	if res.exitCode != 0 {
		t.Fatalf("exit=%d stdout=%q", res.exitCode, res.stdout)
	}
	if res.outputs["new-labels"] != "" || res.outputs["all-labels"] != "" {
		t.Fatalf("unexpected outputs: %v", res.outputs)
	}
	if f.addCalls != 0 {
		t.Fatalf("unexpected add calls: %d", f.addCalls)
	}
}

// P2P-038: more than 100 labels are capped at the first 100.
func TestLabelCapAt100(t *testing.T) {
	f := newFakeGitHub()
	f.changed = []string{"foo.pdf"}
	for i := 0; i < 100; i++ {
		f.prLabels = append(f.prLabels, label{Name: fmt.Sprintf("existing-%03d", i)})
	}
	cfg := "pdf:\n  - changed-files:\n      - any-glob-to-any-file: '*.pdf'\n"
	res := runAction(t, f, actionOptions{config: cfg})
	if res.exitCode != 0 {
		t.Fatalf("exit=%d stdout=%q", res.exitCode, res.stdout)
	}
	if res.outputs["new-labels"] != "" {
		t.Fatalf("new-labels=%q", res.outputs["new-labels"])
	}
	if n := len(splitCSV(res.outputs["all-labels"])); n != 100 {
		t.Fatalf("all-labels has %d entries, want 100: %q", n, res.outputs["all-labels"])
	}
	if !strings.Contains(res.stdout, "100") {
		t.Fatalf("expected excess-label warning, stdout=%q", res.stdout)
	}
}

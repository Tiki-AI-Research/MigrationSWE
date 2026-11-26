package f2pprobe

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"gopkg.in/yaml.v3"

	"github.com/actions/labeler/internal/config"
	"github.com/actions/labeler/internal/core"
	"github.com/actions/labeler/internal/event"
	"github.com/actions/labeler/internal/githubapi"
	"github.com/actions/labeler/internal/inputs"
	"github.com/actions/labeler/internal/labeler"
	"github.com/actions/labeler/internal/match"
	"github.com/actions/labeler/internal/minimatch"
)

// ---------------------------------------------------------------------------
// Fake GitHub API + GraphQL server.
// ---------------------------------------------------------------------------

type fakeGH struct {
	mu sync.Mutex

	prNumber     int
	prNodeID     string
	prLabels     []map[string]string
	changedFiles []string
	filePage2    []string
	linkNext     bool

	configYAML string

	addStatus int
	addCalls  int
	addedRaw  [][]string

	issueLabelPages [][]string

	graphqlCalls int
	graphqlBody  []byte

	restDeletes int
	filePages   []string
}

func (f *fakeGH) handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		path := r.URL.Path

		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(path, "/graphql"):
			body, _ := io.ReadAll(r.Body)
			f.graphqlCalls++
			f.graphqlBody = body
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"data":{}}`))
		case r.Method == http.MethodDelete:
			f.restDeletes++
			w.WriteHeader(http.StatusOK)
		case r.Method == http.MethodGet && strings.Contains(path, "/pulls/") && strings.HasSuffix(path, "/files"):
			f.filePages = append(f.filePages, r.URL.Query().Get("page"))
			if len(f.filePages) > 1 && f.filePage2 != nil {
				_ = json.NewEncoder(w).Encode(filesJSON(f.filePage2))
				return
			}
			if f.linkNext {
				w.Header().Set("Link", fmt.Sprintf(`<https://api.github.com/repos/o/r/pulls/%d/files?page=2>; rel="next"`, f.prNumber))
			}
			_ = json.NewEncoder(w).Encode(filesJSON(f.changedFiles))
		case r.Method == http.MethodGet && strings.Contains(path, "/pulls/"):
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"number":  f.prNumber,
				"node_id": f.prNodeID,
				"labels":  f.prLabels,
			})
		case r.Method == http.MethodGet && strings.Contains(path, "/contents/"):
			encoded := base64.StdEncoding.EncodeToString([]byte(f.configYAML))
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"content":  encoded + "\n",
				"encoding": "base64",
			})
		case r.Method == http.MethodPost && strings.HasSuffix(path, "/labels"):
			f.addCalls++
			var payload struct {
				Labels []string `json:"labels"`
			}
			_ = json.NewDecoder(r.Body).Decode(&payload)
			f.addedRaw = append(f.addedRaw, payload.Labels)
			if f.addStatus >= 400 {
				w.WriteHeader(f.addStatus)
				_, _ = w.Write([]byte(`{"message":"Server Error"}`))
				return
			}
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("[]"))
		case r.Method == http.MethodGet && strings.HasSuffix(path, "/labels"):
			page := 1
			if p := r.URL.Query().Get("page"); p == "2" {
				page = 2
			}
			if page <= len(f.issueLabelPages) {
				names := f.issueLabelPages[page-1]
				out := make([]map[string]string, 0, len(names))
				for _, n := range names {
					out = append(out, map[string]string{"name": n})
				}
				if page < len(f.issueLabelPages) {
					w.Header().Set("Link", fmt.Sprintf(`<https://api.github.com/repos/o/r/issues/%d/labels?page=%d>; rel="next"`, f.prNumber, page+1))
				}
				_ = json.NewEncoder(w).Encode(out)
				return
			}
			_, _ = w.Write([]byte("[]"))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}
}

func filesJSON(files []string) []map[string]string {
	out := make([]map[string]string, 0, len(files))
	for _, f := range files {
		out = append(out, map[string]string{"filename": f})
	}
	return out
}

func newServer(t *testing.T, f *fakeGH) (*httptest.Server, *githubapi.Client) {
	t.Helper()
	srv := httptest.NewServer(f.handler())
	t.Cleanup(srv.Close)
	c := githubapi.NewClientForTest("token", "o", "r", "sha", srv.URL, srv.URL+"/graphql")
	return srv, c
}

// ---------------------------------------------------------------------------
// Action run harness.
// ---------------------------------------------------------------------------

type runResult struct {
	outputs  map[string]string
	warnings string
	addCalls int
	addedRaw [][]string
	err      error
}

func runAction(t *testing.T, f *fakeGH, cfgPath string, pr int, sync bool, dot bool) runResult {
	t.Helper()
	if cfgPath == "" {
		cfgPath = filepath.Join(t.TempDir(), "labeler.yml")
	}
	srv, c := newServer(t, f)
	_ = srv

	outFile := filepath.Join(t.TempDir(), "out.txt")
	t.Setenv("GITHUB_OUTPUT", outFile)

	var buf strings.Builder
	oldOut := core.Out
	core.Out = &buf
	core.Reset()
	defer func() { core.Out = oldOut }()

	ev := &event.Payload{PullRequest: &event.PullRequest{
		Number: pr,
		NodeID: "PR_node_1",
		Head:   event.Branch{Ref: "feature/x"},
		Base:   event.Branch{Ref: "main"},
	}}
	in := inputs.Inputs{Token: "token", ConfigPath: cfgPath, SyncLabels: sync, Dot: dot, PRNumbers: []int{pr}}
	l := &labeler.Labeler{Client: c, Event: ev, Inputs: in}
	err := l.Run()

	res := runResult{outputs: parseOutputs(readFile(outFile)), warnings: buf.String(), err: err}
	f.mu.Lock()
	res.addCalls = f.addCalls
	res.addedRaw = f.addedRaw
	f.mu.Unlock()
	return res
}

func readFile(p string) string {
	data, err := os.ReadFile(p)
	if err != nil {
		return ""
	}
	return string(data)
}

// parseOutputs understands both the heredoc and the legacy ::set-output format.
func parseOutputs(content string) map[string]string {
	out := map[string]string{}
	lines := strings.Split(content, "\n")
	for i := 0; i < len(lines); i++ {
		line := lines[i]
		if strings.HasPrefix(line, "::set-output name=") {
			rest := strings.TrimPrefix(line, "::set-output name=")
			if idx := strings.Index(rest, "::"); idx >= 0 {
				out[rest[:idx]] = rest[idx+2:]
			}
			continue
		}
		if idx := strings.Index(line, "<<"); idx > 0 && (strings.HasPrefix(line, "new-labels<<") || strings.HasPrefix(line, "all-labels<<")) {
			name := line[:idx]
			delim := line[idx+2:]
			var value []string
			j := i + 1
			for ; j < len(lines); j++ {
				if lines[j] == delim {
					break
				}
				value = append(value, lines[j])
			}
			out[name] = strings.Join(value, "\n")
			i = j
		}
	}
	return out
}

// ---------------------------------------------------------------------------
// F2P-001 / F2P-011 / F2P-016: label removal must use one GraphQL mutation.
// ---------------------------------------------------------------------------

func TestF2P001RemoveLabelsUsesGraphQLMutation(t *testing.T) {
	f := &fakeGH{}
	_, c := newServer(t, f)
	if err := c.RemoveLabels("PR_node_1", []string{"LA_stale"}); err != nil {
		t.Fatalf("RemoveLabels returned error: %v", err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.graphqlCalls != 1 {
		t.Fatalf("expected 1 GraphQL call, got %d", f.graphqlCalls)
	}
	if f.restDeletes != 0 {
		t.Fatalf("expected no REST DELETE, got %d", f.restDeletes)
	}
	body := string(f.graphqlBody)
	if !strings.Contains(body, "removeLabelsFromLabelable") {
		t.Fatalf("GraphQL body missing removeLabelsFromLabelable: %s", body)
	}
	if !strings.Contains(body, "labelableId") || !strings.Contains(body, "labelIds") {
		t.Fatalf("GraphQL body missing mutation inputs: %s", body)
	}
	if !strings.Contains(body, "PR_node_1") || !strings.Contains(body, "LA_stale") {
		t.Fatalf("GraphQL body missing node identifiers: %s", body)
	}
}

func TestF2P011RemoveLabelsNotRestDelete(t *testing.T) {
	f := &fakeGH{}
	_, c := newServer(t, f)
	if err := c.RemoveLabels("PR_x", []string{"LA_a", "LA_b"}); err != nil {
		t.Fatalf("RemoveLabels returned error: %v", err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.restDeletes > 0 {
		t.Fatalf("REST DELETE /issues/{n}/labels was used; expected GraphQL only")
	}
	if f.graphqlCalls != 1 {
		t.Fatalf("expected exactly one GraphQL mutation, got %d", f.graphqlCalls)
	}
}

func TestF2P016RemoveLabelsCarriesNodeIDs(t *testing.T) {
	f := &fakeGH{}
	_, c := newServer(t, f)
	if err := c.RemoveLabels("PR_42", []string{"LA_stale_1", "LA_stale_2"}); err != nil {
		t.Fatalf("RemoveLabels returned error: %v", err)
	}
	f.mu.Lock()
	body := string(f.graphqlBody)
	f.mu.Unlock()
	for _, want := range []string{"PR_42", "LA_stale_1", "LA_stale_2"} {
		if !strings.Contains(body, want) {
			t.Fatalf("GraphQL body missing %q: %s", want, body)
		}
	}
}

// ---------------------------------------------------------------------------
// F2P-002: branch patterns are evaluated as JavaScript (ECMAScript) regexps.
// ---------------------------------------------------------------------------

func TestF2P002BranchECMAScriptPattern(t *testing.T) {
	// R033: branch patterns are JavaScript/ECMAScript regular expressions. The
	// release branch matches the semver pattern while main does not, and an
	// ECMAScript-only negative lookahead is honoured (a Go RE2-only engine
	// cannot evaluate it).
	releasePattern := []string{`^release/\d+\.\d+\.\d+$`}
	got, err := match.CheckAnyBranch(match.BranchContext{HeadRef: "release/1.2.3"}, releasePattern, "head")
	if err != nil {
		t.Fatalf("CheckAnyBranch error: %v", err)
	}
	if !got {
		t.Fatalf("expected branch pattern to match release/1.2.3")
	}
	got, err = match.CheckAnyBranch(match.BranchContext{HeadRef: "main"}, releasePattern, "head")
	if err != nil {
		t.Fatalf("CheckAnyBranch error: %v", err)
	}
	if got {
		t.Fatalf("expected pattern not to match main")
	}
	got, err = match.CheckAnyBranch(match.BranchContext{HeadRef: "release/1.2.3"}, []string{`^(?!main$).*`}, "head")
	if err != nil {
		t.Fatalf("CheckAnyBranch error: %v", err)
	}
	if !got {
		t.Fatalf("expected ECMAScript negative lookahead to match release/1.2.3")
	}
}

// ---------------------------------------------------------------------------
// F2P-003 / F2P-006 / F2P-009 / F2P-019: paginated changed-file listing.
// ---------------------------------------------------------------------------

func TestF2P003ListChangedFilesPagination(t *testing.T) {
	f := &fakeGH{prNumber: 7, changedFiles: []string{"a.go"}, filePage2: []string{"b.go"}, linkNext: true}
	_, c := newServer(t, f)
	files, err := c.ListChangedFiles(7)
	if err != nil {
		t.Fatalf("ListChangedFiles error: %v", err)
	}
	if len(files) != 2 || files[0] != "a.go" || files[1] != "b.go" {
		t.Fatalf("expected both pages concatenated, got %v", files)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.filePages) != 2 || f.filePages[1] != "2" {
		t.Fatalf("expected a request for page 2, got pages %v", f.filePages)
	}
}

func TestF2P006ListChangedFilesRequestsNextPage(t *testing.T) {
	f := &fakeGH{prNumber: 3, changedFiles: []string{"x.go"}, filePage2: []string{"y.go"}, linkNext: true}
	_, c := newServer(t, f)
	if _, err := c.ListChangedFiles(3); err != nil {
		t.Fatalf("ListChangedFiles error: %v", err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.filePages) < 2 {
		t.Fatalf("a single-page implementation would stop after page 1: %v", f.filePages)
	}
}

func TestF2P009ListChangedFilesConcatenatesSecondPage(t *testing.T) {
	f := &fakeGH{prNumber: 9, changedFiles: []string{"p1.go"}, filePage2: []string{"p2.go"}, linkNext: true}
	_, c := newServer(t, f)
	files, err := c.ListChangedFiles(9)
	if err != nil {
		t.Fatalf("ListChangedFiles error: %v", err)
	}
	if len(files) != 2 {
		t.Fatalf("expected 2 files from 2 pages, got %v", files)
	}
}

func TestF2P019ListChangedFilesHonorsLinkHeader(t *testing.T) {
	f := &fakeGH{prNumber: 19, changedFiles: []string{"a"}, filePage2: []string{"b", "c"}, linkNext: true}
	_, c := newServer(t, f)
	files, err := c.ListChangedFiles(19)
	if err != nil {
		t.Fatalf("ListChangedFiles error: %v", err)
	}
	if strings.Join(files, ",") != "a,b,c" {
		t.Fatalf("expected a,b,c from Link pagination, got %v", files)
	}
}

// ---------------------------------------------------------------------------
// F2P-004 / F2P-014 / F2P-015 / F2P-017 / F2P-020: label addition recovery.
// ---------------------------------------------------------------------------

func TestF2P004AddLabelsServerErrorRecovered(t *testing.T) {
	f := &fakeGH{prNumber: 1, addStatus: 500, issueLabelPages: [][]string{{"bug", "documentation"}}}
	_, c := newServer(t, f)
	if err := c.AddLabels(1, []string{"bug", "documentation"}); err != nil {
		t.Fatalf("expected recovery when labels are present, got %v", err)
	}
}

func TestF2P014AddLabelsVerifiesListingAfterServerError(t *testing.T) {
	f := &fakeGH{prNumber: 1, addStatus: 500, issueLabelPages: [][]string{{"bug"}}}
	_, c := newServer(t, f)
	if err := c.AddLabels(1, []string{"bug"}); err != nil {
		t.Fatalf("expected success after verifying labels, got %v", err)
	}
}

func TestF2P015AddLabelsRecoveryPagination(t *testing.T) {
	f := &fakeGH{
		prNumber:        1,
		addStatus:       500,
		issueLabelPages: [][]string{{"one", "two"}, {"wanted"}},
	}
	_, c := newServer(t, f)
	if err := c.AddLabels(1, []string{"wanted"}); err != nil {
		t.Fatalf("expected success across label pages, got %v", err)
	}
}

func TestF2P017AddLabelsDoesNotReturnServerErrorWhenApplied(t *testing.T) {
	f := &fakeGH{prNumber: 1, addStatus: 503, issueLabelPages: [][]string{{"release"}}}
	_, c := newServer(t, f)
	err := c.AddLabels(1, []string{"RELEASE"})
	if err != nil {
		t.Fatalf("expected nil error when labels present, got %v", err)
	}
}

func TestF2P020AddLabelsRecoveryUsesCaseInsensitiveNames(t *testing.T) {
	f := &fakeGH{prNumber: 1, addStatus: 500, issueLabelPages: [][]string{{"Bug"}}}
	_, c := newServer(t, f)
	if err := c.AddLabels(1, []string{"bug"}); err != nil {
		t.Fatalf("expected case-insensitive recovery, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// F2P-005: running the Go action writes new-labels and all-labels.
// ---------------------------------------------------------------------------

func TestF2P005ActionWritesOutputs(t *testing.T) {
	f := &fakeGH{
		prNumber:     5,
		changedFiles: []string{"docs/readme.md"},
		configYAML:   "documentation:\n  - changed-files:\n      - any-glob-to-any-file: ['docs/**']\n",
	}
	res := runAction(t, f, "", 5, false, true)
	if res.err != nil {
		t.Fatalf("Run returned error: %v", res.err)
	}
	if res.outputs["new-labels"] != "documentation" {
		t.Fatalf("new-labels = %q, want documentation", res.outputs["new-labels"])
	}
	if res.outputs["all-labels"] != "documentation" {
		t.Fatalf("all-labels = %q, want documentation", res.outputs["all-labels"])
	}
	if res.addCalls != 1 {
		t.Fatalf("expected exactly one label addition, got %d", res.addCalls)
	}
}

// ---------------------------------------------------------------------------
// F2P-007 / F2P-012: configuration validation and unknown-key handling.
// ---------------------------------------------------------------------------

func parseConfig(t *testing.T, yamlText string) (*config.LabelConfigResult, error) {
	t.Helper()
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(yamlText), &doc); err != nil {
		t.Fatalf("yaml.Unmarshal: %v", err)
	}
	return config.GetLabelConfigResultFromObject(&doc)
}

func TestF2P007NegativeLimitRejected(t *testing.T) {
	_, err := parseConfig(t, "changed-files-labels-limit: -1\n")
	if err == nil {
		t.Fatalf("expected error for negative changed-files-labels-limit")
	}
}

func TestF2P012UnknownKeyIgnored(t *testing.T) {
	res, err := parseConfig(t, "mylabel:\n  - changed-files:\n      - any-glob-to-any-file: ['**/*.go']\n    unknown-option: bar\n")
	if err != nil {
		t.Fatalf("unknown config option must be ignored, got %v", err)
	}
	if !res.LabelConfigs.Has("mylabel") {
		t.Fatalf("valid label was not parsed")
	}
}

// ---------------------------------------------------------------------------
// F2P-008: max-files-changed is evaluated before changed-files-labels-limit.
// ---------------------------------------------------------------------------

func TestF2P008MaxFilesChangedSkipsFileLabels(t *testing.T) {
	cfg := "changed-files-labels-limit: 2\nmax-files-changed: 1\nthing:\n  - changed-files:\n      - any-glob-to-any-file: ['**/*.go']\n"
	f := &fakeGH{prNumber: 8, changedFiles: []string{"a.go", "b.go"}, configYAML: cfg}
	res := runAction(t, f, "", 8, false, true)
	if res.err != nil {
		t.Fatalf("Run returned error: %v", res.err)
	}
	if res.addCalls != 0 {
		t.Fatalf("file-based label must be skipped when max-files-changed is exceeded; added %v", res.addedRaw)
	}
	if res.outputs["new-labels"] != "" {
		t.Fatalf("new-labels = %q, want empty", res.outputs["new-labels"])
	}
}

// ---------------------------------------------------------------------------
// F2P-010 / F2P-021 / F2P-022 / F2P-023: base64 content decoding.
// ---------------------------------------------------------------------------

func TestF2P010GetContentDecodesBase64(t *testing.T) {
	f := &fakeGH{configYAML: "hello"}
	_, c := newServer(t, f)
	got, err := c.GetContent("config.yml")
	if err != nil {
		t.Fatalf("GetContent error: %v", err)
	}
	if got != "hello" {
		t.Fatalf("GetContent = %q, want hello", got)
	}
}

func TestF2P021GetContentStripsNewline(t *testing.T) {
	f := &fakeGH{configYAML: "hello"}
	_, c := newServer(t, f)
	got, err := c.GetContent("config.yml")
	if err != nil {
		t.Fatalf("GetContent error: %v", err)
	}
	if strings.Contains(got, "\n") || got == "" || got == "aGVsbG8=" {
		t.Fatalf("GetContent = %q, want base64-decoded value without newline", got)
	}
}

func TestF2P022GetContentReturnsDecodedValue(t *testing.T) {
	f := &fakeGH{configYAML: "hello"}
	_, c := newServer(t, f)
	got, err := c.GetContent("config.yml")
	if err != nil {
		t.Fatalf("GetContent error: %v", err)
	}
	if got != "hello" {
		t.Fatalf("GetContent = %q, want hello", got)
	}
}

func TestF2P023GetContentRejectsRawEncodedString(t *testing.T) {
	f := &fakeGH{configYAML: "hello"}
	_, c := newServer(t, f)
	got, err := c.GetContent("config.yml")
	if err != nil {
		t.Fatalf("GetContent error: %v", err)
	}
	if got == base64.StdEncoding.EncodeToString([]byte("hello")) {
		t.Fatalf("GetContent returned raw encoded string")
	}
}

// ---------------------------------------------------------------------------
// F2P-013: label set capped at 100 with a warning for the excess.
// ---------------------------------------------------------------------------

func TestF2P013AllLabelsCappedAt100(t *testing.T) {
	var b strings.Builder
	for i := 0; i < 150; i++ {
		fmt.Fprintf(&b, "label-%03d:\n  - changed-files:\n      - any-glob-to-any-file: ['**/*.go']\n", i)
	}
	f := &fakeGH{prNumber: 13, changedFiles: []string{"x.go"}, configYAML: b.String()}
	res := runAction(t, f, "", 13, false, true)
	if res.err != nil {
		t.Fatalf("Run returned error: %v", res.err)
	}
	names := strings.Split(res.outputs["all-labels"], ",")
	if len(names) != 100 {
		t.Fatalf("all-labels must contain the first 100 labels, got %d", len(names))
	}
	if res.addCalls != 1 || len(res.addedRaw[0]) != 100 {
		t.Fatalf("expected one addition of the first 100 labels, got %v", res.addedRaw)
	}
	excess := 0
	for i := 100; i < 150; i++ {
		if strings.Contains(res.warnings, fmt.Sprintf("label-%03d", i)) {
			excess++
		}
	}
	if excess != 50 {
		t.Fatalf("warning must list the 50 excess labels, found %d", excess)
	}
}

// ---------------------------------------------------------------------------
// F2P-018: non-printable pr-number characters escaped as \xNN.
// ---------------------------------------------------------------------------

func TestF2P018EscapeNonPrintable(t *testing.T) {
	got := inputs.EscapeNonPrintable("12\x0734")
	if !strings.Contains(got, `\x07`) {
		t.Fatalf("expected \\x07 escape, got %q", got)
	}
	if strings.ContainsRune(got, '\x07') {
		t.Fatalf("raw control byte was emitted: %q", got)
	}
}

// ---------------------------------------------------------------------------
// F2P-024: minimatch dot option and ! negation semantics.
// ---------------------------------------------------------------------------

func TestF2P024MinimatchDotAndNegation(t *testing.T) {
	if minimatch.NewMatcher("**/*.ts", false).Match(".hidden/x.ts") {
		t.Fatalf("dot=false must not match hidden path")
	}
	if !minimatch.NewMatcher("**/*.ts", true).Match(".hidden/x.ts") {
		t.Fatalf("dot=true must match hidden path")
	}
	if !minimatch.NewMatcher("!docs/**", true).Match("src/a.ts") {
		t.Fatalf("negated pattern must match a non-docs path")
	}
	if minimatch.NewMatcher("!docs/**", true).Match("docs/a.md") {
		t.Fatalf("negated pattern must not match a docs path")
	}
}

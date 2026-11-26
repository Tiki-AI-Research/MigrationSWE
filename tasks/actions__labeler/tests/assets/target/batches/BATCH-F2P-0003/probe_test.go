// Package batchf2p contains the API-behaviour F2P probes owned by
// BATCH-F2P-0003.
//
// These probes exercise the migrated action through the Go packages the
// instruction fixes as its observed surface.  Repository-level technology
// postconditions live in BATCH-F2P-0003-static so an unrelated probe
// compilation failure can never invalidate them.
package batchf2p

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/actions/labeler/internal/event"
	"github.com/actions/labeler/internal/githubapi"
	"github.com/actions/labeler/internal/inputs"
	"github.com/actions/labeler/internal/labeler"
)

type fakeGitHub struct {
	changed  []string
	prNodeID string
	prLabels []map[string]string
	added    [][]string
	removed  []map[string]interface{}
	content  string
}

func (f *fakeGitHub) handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
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
			f.removed = append(f.removed, map[string]interface{}{
				"labelableId": body.Variables.LabelableID,
				"labelIds":    body.Variables.LabelIDs,
			})
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"data": map[string]interface{}{}})
		case len(parts) == 5 && parts[3] == "pulls":
			labels := make([]map[string]string, 0, len(f.prLabels))
			labels = append(labels, f.prLabels...)
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"number": 123, "node_id": f.prNodeID, "labels": labels,
			})
		case len(parts) == 6 && parts[3] == "pulls" && parts[5] == "files":
			if f.changed != nil {
				files := make([]map[string]string, 0, len(f.changed))
				for _, c := range f.changed {
					files = append(files, map[string]string{"filename": c})
				}
				_ = json.NewEncoder(w).Encode(files)
				return
			}
			page := r.URL.Query().Get("page")
			var files []map[string]string
			if page == "" || page == "1" {
				for i := 0; i < 100; i++ {
					files = append(files, map[string]string{"filename": "file-" + itoa(i)})
				}
				w.Header().Set("Link", `</repos/monalisa/helloworld/pulls/123/files?per_page=100&page=2>; rel="next"`)
			} else {
				for i := 100; i < 150; i++ {
					files = append(files, map[string]string{"filename": "file-" + itoa(i)})
				}
			}
			_ = json.NewEncoder(w).Encode(files)
		case len(parts) >= 5 && parts[3] == "contents":
			_ = json.NewEncoder(w).Encode(map[string]string{
				"content":  base64.StdEncoding.EncodeToString([]byte(f.content)) + "\n",
				"encoding": "base64",
			})
		case len(parts) == 6 && parts[3] == "issues" && parts[5] == "labels":
			if r.Method == http.MethodPost {
				var body struct {
					Labels []string `json:"labels"`
				}
				_ = json.NewDecoder(r.Body).Decode(&body)
				f.added = append(f.added, body.Labels)
				_ = json.NewEncoder(w).Encode([]map[string]string{})
				return
			}
			labels := make([]map[string]string, 0, len(f.prLabels))
			for _, l := range f.prLabels {
				labels = append(labels, map[string]string{"name": l["name"]})
			}
			_ = json.NewEncoder(w).Encode(labels)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}

func readOutputs(t *testing.T, path string) map[string]string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		return map[string]string{}
	}
	lines := strings.Split(string(data), "\n")
	out := map[string]string{}
	for i := 0; i < len(lines); i++ {
		// Support both documented GITHUB_OUTPUT encodings: the heredoc form
		// ``name<<DELIM`` and the single-line ``name=value`` form.
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

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func runLabeler(t *testing.T, f *fakeGitHub, configPath string, dot bool, head string) (map[string]string, error) {
	t.Helper()
	outFile := filepath.Join(t.TempDir(), "output")
	t.Setenv("GITHUB_OUTPUT", outFile)
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	client := githubapi.NewClientForTest("token", "monalisa", "helloworld", "sha", srv.URL, srv.URL+"/graphql")
	ev := &event.Payload{PullRequest: &event.PullRequest{
		Number: 123,
		Head:   event.Branch{Ref: head},
		Base:   event.Branch{Ref: "main"},
	}}
	l := &labeler.Labeler{Client: client, Event: ev, Inputs: inputs.Inputs{
		Token:      "token",
		ConfigPath: configPath,
		SyncLabels: false,
		Dot:        dot,
		PRNumbers:  []int{123},
	}}
	err := l.Run()
	return readOutputs(t, outFile), err
}

// F2P-068: given preexisting labels [a] and matching configuration order
// [b,a,c], all-labels is "a,b,c" (existing first, then newly matched labels in
// configuration order).
func TestF2P068_AllLabelsOrdering(t *testing.T) {
	cfg := filepath.Join(t.TempDir(), "ordering.yml")
	writeFile(t, cfg, "b:\n  - changed-files:\n      - any-glob-to-any-file: ['*.txt']\n"+
		"a:\n  - changed-files:\n      - any-glob-to-any-file: ['*.txt']\n"+
		"c:\n  - changed-files:\n      - any-glob-to-any-file: ['*.txt']\n")
	f := &fakeGitHub{changed: []string{"x.txt"}, prLabels: []map[string]string{{"name": "a"}}}
	out, err := runLabeler(t, f, cfg, true, "main")
	if err != nil {
		t.Fatal(err)
	}
	if out["all-labels"] != "a,b,c" {
		t.Fatalf("all-labels = %q, want %q", out["all-labels"], "a,b,c")
	}
}

// F2P-055: a Link header with rel=next makes ListChangedFiles request page 2
// and concatenate both pages.
func TestF2P055_PaginatedChangedFiles(t *testing.T) {
	f := &fakeGitHub{}
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	client := githubapi.NewClientForTest("token", "monalisa", "helloworld", "sha", srv.URL, srv.URL+"/graphql")
	files, err := client.ListChangedFiles(123)
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 150 {
		t.Fatalf("ListChangedFiles returned %d files, want 150", len(files))
	}
	if files[0] != "file-0" || files[149] != "file-149" {
		t.Fatalf("unexpected concatenation: first=%q last=%q", files[0], files[149])
	}
}

// F2P-056/F2P-061/F2P-062: GetContent strips the newline and base64-decodes the
// contents API payload.
func TestF2P056_GetContentBase64Decoding(t *testing.T) {
	f := &fakeGitHub{content: "hello"}
	srv := httptest.NewServer(f.handler())
	defer srv.Close()
	client := githubapi.NewClientForTest("token", "monalisa", "helloworld", "sha", srv.URL, srv.URL+"/graphql")
	got, err := client.GetContent(".github/labeler.yml")
	if err != nil {
		t.Fatal(err)
	}
	if got != "hello" {
		t.Fatalf("GetContent = %q, want %q", got, "hello")
	}
}

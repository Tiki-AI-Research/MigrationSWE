package main

// Contract probe for BATCH-CONTRACT-0001.
//
// This file is authored by the CONTRACT batch and is intended to be copied into
// the migrated Go repository (package main) by the INTEGRATION runner. It
// exercises the public contract of the release action and asserts the observable
// behaviour that must match the pre-migration TypeScript implementation.
//
// Every check is a real invocation of the migrated Go code; nothing is skipped
// and no comparison is weakened.

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// Contract probe doubles (uniquely named to avoid clashing with the repository
// test helpers when this file is compiled alongside them).
// ---------------------------------------------------------------------------

type cpInputGetter struct {
	values map[string]string
}

func (g cpInputGetter) GetInput(name string) string {
	return strings.TrimSpace(g.values[name])
}

func (g cpInputGetter) GetBooleanInput(name string) (bool, error) {
	value, ok := g.values[name]
	if !ok {
		return false, nil
	}
	value = strings.TrimSpace(value)
	switch value {
	case "true", "True", "TRUE":
		return true, nil
	case "false", "False", "FALSE":
		return false, nil
	}
	return false, fmt.Errorf("Input does not meet YAML 1.2 \"Core Schema\" specification: %s", name)
}

type cpOutputs struct {
	release *ReleaseData
	assets  map[string]string
}

func (o *cpOutputs) ApplyReleaseData(data ReleaseData)     { o.release = &data }
func (o *cpOutputs) ApplyAssetUrls(urls map[string]string) { o.assets = urls }

type cpUploader struct {
	calls int
	urls  map[string]string
}

func (u *cpUploader) UploadArtifacts(artifacts []Artifact, releaseId int64, uploadUrl string) (map[string]string, error) {
	u.calls++
	return u.urls, nil
}

type cpDestroyer struct{ calls int }

func (d *cpDestroyer) DestroyArtifacts(releaseId int64) error {
	d.calls++
	return nil
}

type cpSkipper struct {
	skip bool
	err  error
}

func (s *cpSkipper) ShouldSkip() (bool, error) { return s.skip, s.err }

type cpReleases struct {
	createFn     func(tag string, body *string, commitHash *string, discussionCategory *string, draft *bool, makeLatest *string, name *string, prerelease *bool) (ReleaseData, error)
	updateFn     func(id int64, tag string, body *string, commitHash *string, discussionCategory *string, draft *bool, makeLatest *string, name *string, prerelease *bool) (ReleaseData, error)
	getByTagFn   func(tag string) (*ReleaseData, error)
	genNotesFn   func(tag string, previousTag *string, targetCommitish *string) (GenerateReleaseNotesResponse, error)
	listFn       func() (*ListReleasesResponse, error)
	listAssetsFn func(releaseId int64) ([]ReleaseAsset, error)
	deleteFn     func(assetId int64) error
	uploadFn     func(assetUrl string, contentLength int64, contentType string, file io.Reader, name string, releaseId int64) (UploadArtifactResponse, error)
}

func (f *cpReleases) Create(tag string, body *string, commitHash *string, discussionCategory *string, draft *bool, makeLatest *string, name *string, prerelease *bool) (ReleaseData, error) {
	if f.createFn != nil {
		return f.createFn(tag, body, commitHash, discussionCategory, draft, makeLatest, name, prerelease)
	}
	return ReleaseData{}, nil
}

func (f *cpReleases) Update(id int64, tag string, body *string, commitHash *string, discussionCategory *string, draft *bool, makeLatest *string, name *string, prerelease *bool) (ReleaseData, error) {
	if f.updateFn != nil {
		return f.updateFn(id, tag, body, commitHash, discussionCategory, draft, makeLatest, name, prerelease)
	}
	return ReleaseData{}, nil
}

func (f *cpReleases) GetByTag(tag string) (*ReleaseData, error) {
	if f.getByTagFn != nil {
		return f.getByTagFn(tag)
	}
	return nil, nil
}

func (f *cpReleases) GenerateReleaseNotes(tag string, previousTag *string, targetCommitish *string) (GenerateReleaseNotesResponse, error) {
	if f.genNotesFn != nil {
		return f.genNotesFn(tag, previousTag, targetCommitish)
	}
	return GenerateReleaseNotesResponse{}, nil
}

func (f *cpReleases) ListReleases() (*ListReleasesResponse, error) {
	if f.listFn != nil {
		return f.listFn()
	}
	empty := []ReleaseData{}
	return &ListReleasesResponse{Data: &empty}, nil
}

func (f *cpReleases) ListArtifactsForRelease(releaseId int64) ([]ReleaseAsset, error) {
	if f.listAssetsFn != nil {
		return f.listAssetsFn(releaseId)
	}
	return []ReleaseAsset{}, nil
}

func (f *cpReleases) DeleteArtifact(assetId int64) error {
	if f.deleteFn != nil {
		return f.deleteFn(assetId)
	}
	return nil
}

func (f *cpReleases) UploadArtifact(assetUrl string, contentLength int64, contentType string, file io.Reader, name string, releaseId int64) (UploadArtifactResponse, error) {
	if f.uploadFn != nil {
		return f.uploadFn(assetUrl, contentLength, contentType, file, name, releaseId)
	}
	return UploadArtifactResponse{}, nil
}

type cpUploadError struct {
	message string
	status  int
}

func (e cpUploadError) Error() string   { return e.message }
func (e cpUploadError) StatusCode() int { return e.status }

type cpGlobber struct {
	patterns *[]string
	ret      []string
}

func (g *cpGlobber) Glob(pattern string) ([]string, error) {
	*g.patterns = append(*g.patterns, pattern)
	return g.ret, nil
}

// ---------------------------------------------------------------------------
// Small local helpers
// ---------------------------------------------------------------------------

func cpStr(s string) *string { return &s }

func cpBool(b bool) *bool { return &b }

func cpWriteFile(t *testing.T, name string, content string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func cpReadActionYML(t *testing.T) string {
	t.Helper()
	data, err := os.ReadFile("action.yml")
	if err != nil {
		t.Fatalf("read action.yml: %v", err)
	}
	return string(data)
}

func cpTopLevelKeys(text string, section string) []string {
	var keys []string
	inSection := false
	for _, line := range strings.Split(text, "\n") {
		if strings.HasPrefix(line, section+":") {
			inSection = true
			continue
		}
		if strings.HasPrefix(line, " ") || line == "" {
			if inSection {
				trimmed := strings.TrimSuffix(line, "\r")
				if strings.HasPrefix(trimmed, "  ") && !strings.HasPrefix(trimmed, "    ") {
					name := strings.TrimSuffix(strings.TrimSpace(trimmed), ":")
					if name != "" {
						keys = append(keys, name)
					}
				}
			}
			continue
		}
		inSection = false
	}
	return keys
}

func cpSortedEqual(a, b []string) bool {
	x := append([]string{}, a...)
	y := append([]string{}, b...)
	sort.Strings(x)
	sort.Strings(y)
	return reflect.DeepEqual(x, y)
}

func cpNormalizeActionYml(text string) string {
	lines := strings.Split(text, "\n")
	out := make([]string, 0, len(lines))
	skip := false
	for _, line := range lines {
		if strings.HasPrefix(line, "runs:") {
			skip = true
			continue
		}
		if strings.HasPrefix(line, "branding:") {
			skip = false
		}
		if !skip {
			out = append(out, line)
		}
	}
	return strings.TrimRight(strings.Join(out, "\n"), "\n") + "\n"
}

// ---------------------------------------------------------------------------
// Contract checks
// ---------------------------------------------------------------------------

func TestContractPublicSurface(t *testing.T) {
	t.Run("CONTRACT-001", func(t *testing.T) {
		uploader := &cpUploader{urls: map[string]string{"a": "u"}}
		destroyer := &cpDestroyer{}
		outputs := &cpOutputs{}
		releases := &cpReleases{createFn: func(string, *string, *string, *string, *bool, *string, *string, *bool) (ReleaseData, error) {
			return ReleaseData{ID: 7, HTMLURL: "h", UploadURL: "u"}, nil
		}}
		inputs := &Inputs{
			Tag:               "v1",
			CreatedReleaseName: cpStr("v1"),
			CreatedReleaseBody: cpStr("body"),
			RemoveArtifacts:   true,
			Artifacts:         []Artifact{NewArtifact(cpWriteFile(t, "a.bin", "data"), "raw")},
		}
		action := NewAction(inputs, outputs, releases, uploader, destroyer, &cpSkipper{})
		if err := action.Perform(); err != nil {
			t.Fatalf("perform: %v", err)
		}
		// create, destroy, upload all observed and outputs reported.
		if uploader.calls != 1 || destroyer.calls != 1 {
			t.Fatalf("upload=%d destroy=%d", uploader.calls, destroyer.calls)
		}
		if outputs.release == nil || outputs.release.ID != 7 || outputs.assets["a"] != "u" {
			t.Fatalf("outputs = %+v", outputs)
		}
	})

	t.Run("CONTRACT-002", func(t *testing.T) {
		got := map[string]string{}
		outputs := NewCoreOutputsWith(func(name, value string) {
			got[name] = value
		})
		outputs.ApplyReleaseData(ReleaseData{ID: 1, HTMLURL: "h", UploadURL: "u", TarballURL: cpStr("t"), ZipballURL: cpStr("z")})
		outputs.ApplyAssetUrls(map[string]string{})
		names := make([]string, 0, len(got))
		for name := range got {
			names = append(names, name)
		}
		want := []string{"id", "html_url", "upload_url", "tarball_url", "zipball_url", "assets"}
		if !cpSortedEqual(names, want) {
			t.Fatalf("output names = %v", names)
		}
	})

	t.Run("CONTRACT-003", func(t *testing.T) {
		text := cpReadActionYML(t)
		got := cpTopLevelKeys(text, "inputs")
		want := []string{
			"allowUpdates", "artifactErrorsFailBuild", "artifact", "artifacts", "artifactContentType",
			"body", "bodyFile", "commit", "discussionCategory", "draft", "generateReleaseNotes",
			"generateReleaseNotesPreviousTag", "immutableCreate", "makeLatest", "name", "omitBody",
			"omitBodyDuringUpdate", "omitDraftDuringUpdate", "omitName", "omitNameDuringUpdate",
			"omitPrereleaseDuringUpdate", "owner", "prerelease", "removeArtifacts", "replacesArtifacts",
			"repo", "skipIfReleaseExists", "tag", "token", "updateOnlyUnreleased",
		}
		if !cpSortedEqual(got, want) {
			t.Fatalf("action.yml inputs = %v", got)
		}
	})

	t.Run("CONTRACT-004", func(t *testing.T) {
		text := cpReadActionYML(t)
		got := cpTopLevelKeys(text, "outputs")
		want := []string{"id", "html_url", "upload_url", "tarball_url", "zipball_url", "assets"}
		if !cpSortedEqual(got, want) {
			t.Fatalf("action.yml outputs = %v", got)
		}
	})

	t.Run("CONTRACT-005", func(t *testing.T) {
		var seen []string
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			body, _ := io.ReadAll(r.Body)
			entry := r.Method + " " + r.URL.Path
			if strings.Contains(r.URL.Path, "/assets") && r.Method == http.MethodGet {
				if !strings.Contains(r.URL.RawQuery, "per_page=100") {
					entry += " missing-per_page"
				}
			}
			seen = append(seen, entry)
			if len(body) > 0 {
				w.Header().Set("content-type", "application/json")
			}
			if strings.HasSuffix(r.URL.Path, "/upload") {
				_, _ = w.Write([]byte(`{"browser_download_url":"u"}`))
				return
			}
			if r.Method == http.MethodGet && (strings.HasSuffix(r.URL.Path, "/releases") || strings.Contains(r.URL.Path, "/assets")) {
				_, _ = w.Write([]byte(`[]`))
				return
			}
			_, _ = w.Write([]byte(`{"id":1}`))
		}))
		defer server.Close()
		t.Setenv("GITHUB_API_URL", server.URL)

		inputs := &Inputs{Owner: "o", Repo: "r", Token: "t"}
		releases := NewGithubReleasesWithClient(inputs, server.Client())
		if _, err := releases.Create("v1", nil, nil, nil, nil, nil, nil, nil); err != nil {
			t.Fatal(err)
		}
		if _, err := releases.Update(1, "v1", nil, nil, nil, nil, nil, nil, nil); err != nil {
			t.Fatal(err)
		}
		if _, err := releases.GetByTag("v1"); err != nil {
			t.Fatal(err)
		}
		if _, err := releases.GenerateReleaseNotes("v1", nil, nil); err != nil {
			t.Fatal(err)
		}
		if _, err := releases.ListReleases(); err != nil {
			t.Fatal(err)
		}
		if _, err := releases.ListArtifactsForRelease(1); err != nil {
			t.Fatal(err)
		}
		if err := releases.DeleteArtifact(5); err != nil {
			t.Fatal(err)
		}
		if _, err := releases.UploadArtifact(server.URL+"/upload", 4, "raw", strings.NewReader("data"), "art1", 1); err != nil {
			t.Fatal(err)
		}

		want := []string{
			"POST /repos/o/r/releases",
			"PATCH /repos/o/r/releases/1",
			"GET /repos/o/r/releases/tags/v1",
			"POST /repos/o/r/releases/generate-notes",
			"GET /repos/o/r/releases",
			"GET /repos/o/r/releases/1/assets",
			"DELETE /repos/o/r/releases/assets/5",
			"POST /upload",
		}
		if !cpSortedEqual(seen, want) {
			t.Fatalf("requests = %v", seen)
		}
	})

	t.Run("CONTRACT-006", func(t *testing.T) {
		globber := NewFileArtifactGlobber()
		getter := cpInputGetter{values: map[string]string{"token": "t"}}
		inputs, err := LoadInputs(getter, globber, ActionContext{Ref: "refs/tags/v1.2.3"})
		if err != nil {
			t.Fatal(err)
		}
		if inputs.Tag != "v1.2.3" {
			t.Fatalf("tag = %q", inputs.Tag)
		}
		_, err = LoadInputs(getter, globber, ActionContext{Ref: "refs/heads/main"})
		if err == nil || err.Error() != "No tag found in ref or input!" {
			t.Fatalf("err = %v", err)
		}
	})

	t.Run("CONTRACT-007", func(t *testing.T) {
		globber := NewFileArtifactGlobber()
		getter := cpInputGetter{values: map[string]string{"tag": "v1"}}
		if _, err := LoadInputs(getter, globber, ActionContext{}); err == nil || err.Error() != "Input required and not supplied: token" {
			t.Fatalf("err = %v", err)
		}
		getter = cpInputGetter{values: map[string]string{"tag": "v1", "token": "secret"}}
		inputs, err := LoadInputs(getter, globber, ActionContext{})
		if err != nil {
			t.Fatal(err)
		}
		if inputs.Token != "secret" {
			t.Fatalf("token = %q", inputs.Token)
		}
	})

	t.Run("CONTRACT-008", func(t *testing.T) {
		path := cpWriteFile(t, "app.bin", "12345")
		globber := NewFileArtifactGlobber()
		getter := cpInputGetter{values: map[string]string{"token": "t", "tag": "v1", "artifact": path}}
		inputs, err := LoadInputs(getter, globber, ActionContext{})
		if err != nil {
			t.Fatal(err)
		}
		if len(inputs.Artifacts) != 1 {
			t.Fatalf("artifacts = %v", inputs.Artifacts)
		}
		if inputs.Artifacts[0].ContentType != "raw" || inputs.Artifacts[0].Name != "app.bin" {
			t.Fatalf("artifact = %+v", inputs.Artifacts[0])
		}
	})

	t.Run("CONTRACT-009", func(t *testing.T) {
		globber := NewFileArtifactGlobber()
		getter := cpInputGetter{values: map[string]string{"token": "t", "tag": "v1"}}
		inputs, err := LoadInputs(getter, globber, ActionContext{Owner: "ctxowner", Repo: "ctxrepo"})
		if err != nil {
			t.Fatal(err)
		}
		if inputs.Owner != "ctxowner" || inputs.Repo != "ctxrepo" {
			t.Fatalf("owner=%q repo=%q", inputs.Owner, inputs.Repo)
		}
		getter = cpInputGetter{values: map[string]string{"token": "t", "tag": "v1", "owner": "o", "repo": "r"}}
		inputs, err = LoadInputs(getter, globber, ActionContext{Owner: "ctxowner", Repo: "ctxrepo"})
		if err != nil {
			t.Fatal(err)
		}
		if inputs.Owner != "o" || inputs.Repo != "r" {
			t.Fatalf("owner=%q repo=%q", inputs.Owner, inputs.Repo)
		}
	})

	t.Run("CONTRACT-010", func(t *testing.T) {
		globber := NewFileArtifactGlobber()
		for _, value := range []string{"legacy", "true", "false"} {
			getter := cpInputGetter{values: map[string]string{"token": "t", "tag": "v1", "makeLatest": value}}
			inputs, err := LoadInputs(getter, globber, ActionContext{})
			if err != nil {
				t.Fatal(err)
			}
			if inputs.MakeLatest == nil || *inputs.MakeLatest != value {
				t.Fatalf("makeLatest(%q) = %v", value, inputs.MakeLatest)
			}
		}
		getter := cpInputGetter{values: map[string]string{"token": "t", "tag": "v1", "makeLatest": "sometimes"}}
		inputs, err := LoadInputs(getter, globber, ActionContext{})
		if err != nil {
			t.Fatal(err)
		}
		if inputs.MakeLatest != nil {
			t.Fatalf("makeLatest = %v", *inputs.MakeLatest)
		}
	})

	t.Run("CONTRACT-011", func(t *testing.T) {
		globber := NewFileArtifactGlobber()
		getter := cpInputGetter{values: map[string]string{"token": "t", "tag": "v1", "allowUpdates": " true ", "draft": "TRUE"}}
		inputs, err := LoadInputs(getter, globber, ActionContext{})
		if err != nil {
			t.Fatal(err)
		}
		if !inputs.AllowUpdates || inputs.CreatedDraft {
			t.Fatalf("allowUpdates=%v draft=%v", inputs.AllowUpdates, inputs.CreatedDraft)
		}
		getter = cpInputGetter{values: map[string]string{"token": "t", "tag": "v1", "skipIfReleaseExists": "TRUE"}}
		inputs, err = LoadInputs(getter, globber, ActionContext{})
		if err != nil {
			t.Fatal(err)
		}
		if !inputs.SkipIfReleaseExists {
			t.Fatal("skipIfReleaseExists TRUE not accepted")
		}
		getter = cpInputGetter{values: map[string]string{"token": "t", "tag": "v1", "skipIfReleaseExists": "yes"}}
		if _, err = LoadInputs(getter, globber, ActionContext{}); err == nil {
			t.Fatal("expected error for skipIfReleaseExists=yes")
		}
	})

	t.Run("CONTRACT-012", func(t *testing.T) {
		base := func() *Inputs {
			return &Inputs{Tag: "v1", CreatedReleaseName: cpStr("v1"), CreatedReleaseBody: cpStr("b")}
		}
		// allowUpdates true, existing release -> update.
		updated := 0
		releases := &cpReleases{
			getByTagFn: func(string) (*ReleaseData, error) { return &ReleaseData{ID: 3, Draft: true}, nil },
			updateFn: func(int64, string, *string, *string, *string, *bool, *string, *string, *bool) (ReleaseData, error) {
				updated++
				return ReleaseData{ID: 3}, nil
			},
		}
		inputs := base()
		inputs.AllowUpdates = true
		if err := NewAction(inputs, &cpOutputs{}, releases, &cpUploader{}, &cpDestroyer{}, &cpSkipper{}).Perform(); err != nil {
			t.Fatal(err)
		}
		if updated != 1 {
			t.Fatalf("updated=%d", updated)
		}
		// allowUpdates true, 404, no draft -> create.
		created := 0
		releases = &cpReleases{
			getByTagFn: func(string) (*ReleaseData, error) { return nil, &GithubError{Status: 404, Message: "Not Found"} },
			listFn: func() (*ListReleasesResponse, error) {
				empty := []ReleaseData{}
				return &ListReleasesResponse{Data: &empty}, nil
			},
			createFn: func(string, *string, *string, *string, *bool, *string, *string, *bool) (ReleaseData, error) {
				created++
				return ReleaseData{ID: 4}, nil
			},
		}
		if err := NewAction(base(), &cpOutputs{}, releases, &cpUploader{}, &cpDestroyer{}, &cpSkipper{}).Perform(); err != nil {
			t.Fatal(err)
		}
		if created != 1 {
			t.Fatalf("created=%d", created)
		}
		// allowUpdates false -> create.
		created = 0
		releases = &cpReleases{createFn: func(string, *string, *string, *string, *bool, *string, *string, *bool) (ReleaseData, error) {
			created++
			return ReleaseData{ID: 5}, nil
		}}
		if err := NewAction(base(), &cpOutputs{}, releases, &cpUploader{}, &cpDestroyer{}, &cpSkipper{}).Perform(); err != nil {
			t.Fatal(err)
		}
		if created != 1 {
			t.Fatalf("created=%d", created)
		}
	})

	t.Run("CONTRACT-013", func(t *testing.T) {
		created := 0
		releases := &cpReleases{
			getByTagFn: func(string) (*ReleaseData, error) { return nil, &GithubError{Status: 500, Message: "boom"} },
			createFn: func(string, *string, *string, *string, *bool, *string, *string, *bool) (ReleaseData, error) {
				created++
				return ReleaseData{}, nil
			},
		}
		inputs := &Inputs{Tag: "v1", AllowUpdates: true, CreatedReleaseName: cpStr("v1")}
		err := NewAction(inputs, &cpOutputs{}, releases, &cpUploader{}, &cpDestroyer{}, &cpSkipper{}).Perform()
		if err == nil {
			t.Fatal("expected propagated error")
		}
		if created != 0 {
			t.Fatalf("created=%d", created)
		}
	})

	t.Run("CONTRACT-014", func(t *testing.T) {
		releases := &cpReleases{getByTagFn: func(string) (*ReleaseData, error) { return &ReleaseData{ID: 1}, nil }}
		skip, err := NewReleaseActionSkipper(true, releases, "v1").ShouldSkip()
		if err != nil || !skip {
			t.Fatalf("skip=%v err=%v", skip, err)
		}
		releases = &cpReleases{getByTagFn: func(string) (*ReleaseData, error) { return &ReleaseData{ID: 1, Draft: true}, nil }}
		skip, err = NewReleaseActionSkipper(true, releases, "v1").ShouldSkip()
		if err != nil || skip {
			t.Fatalf("skip=%v err=%v", skip, err)
		}
		releases = &cpReleases{getByTagFn: func(string) (*ReleaseData, error) { return nil, errors.New("boom") }}
		skip, err = NewReleaseActionSkipper(true, releases, "v1").ShouldSkip()
		if err != nil || skip {
			t.Fatalf("skip=%v err=%v", skip, err)
		}
	})

	t.Run("CONTRACT-015", func(t *testing.T) {
		if err := NewReleaseValidator(true).ValidateReleaseUpdate(ReleaseData{Draft: false, Prerelease: false, Name: cpStr("v1")}); err == nil {
			t.Fatal("expected failure for published release")
		}
		if err := NewReleaseValidator(true).ValidateReleaseUpdate(ReleaseData{Draft: true, Prerelease: false, Name: cpStr("v1")}); err != nil {
			t.Fatalf("draft: %v", err)
		}
		if err := NewReleaseValidator(true).ValidateReleaseUpdate(ReleaseData{Draft: false, Prerelease: true, Name: cpStr("v1")}); err != nil {
			t.Fatalf("prerelease: %v", err)
		}
	})

	t.Run("CONTRACT-016", func(t *testing.T) {
		parts := artifactSplitPattern.Split("a.txt,b.txt\nc.txt", -1)
		if !reflect.DeepEqual(parts, []string{"a.txt", "b.txt", "c.txt"}) {
			t.Fatalf("split = %v", parts)
		}
		if normalizePath("a/b") != "a/b" {
			t.Fatalf("normalizePath = %q", normalizePath("a/b"))
		}
		if expandTildeWithHome("~/x", "/home/u") != "/home/u/x" {
			t.Fatalf("tilde home = %q", expandTildeWithHome("~/x", "/home/u"))
		}
		if expandTildeWithHome("~other/x", "/home/u") != "~other/x" {
			t.Fatalf("tilde user = %q", expandTildeWithHome("~other/x", "/home/u"))
		}
		if expandTildeWithHome("~", "/home/u") != "/home/u" {
			t.Fatalf("bare tilde = %q", expandTildeWithHome("~", "/home/u"))
		}
	})

	t.Run("CONTRACT-017", func(t *testing.T) {
		globber := NewFileArtifactGlobber()
		if _, err := globber.GlobArtifactString("definitely-not-here-*.zzz", "raw", true); err == nil {
			t.Fatal("expected error for unmatched glob with errorsFailBuild=true")
		}
		artifacts, err := globber.GlobArtifactString("definitely-not-here-*.zzz", "raw", false)
		if err != nil || len(artifacts) != 0 {
			t.Fatalf("artifacts=%v err=%v", artifacts, err)
		}
		dir := t.TempDir()
		artifacts, err = globber.GlobArtifactString(dir, "raw", false)
		if err != nil || len(artifacts) != 0 {
			t.Fatalf("directory filtered: artifacts=%v err=%v", artifacts, err)
		}
		if _, err := globber.GlobArtifactString(dir, "raw", true); err == nil {
			t.Fatal("expected error for directory with errorsFailBuild=true")
		}
	})

	t.Run("CONTRACT-018", func(t *testing.T) {
		path := cpWriteFile(t, "pkg.tar.gz", "1234567")
		artifact := NewArtifact(path, "raw")
		length, err := artifact.ContentLength()
		if err != nil {
			t.Fatal(err)
		}
		if artifact.Name != "pkg.tar.gz" || length != 7 {
			t.Fatalf("name=%q length=%d", artifact.Name, length)
		}
	})

	t.Run("CONTRACT-019", func(t *testing.T) {
		artifacts := []Artifact{NewArtifact(cpWriteFile(t, "art1", "x"), "raw")}
		var deleted []int64
		releases := &cpReleases{
			listAssetsFn: func(int64) ([]ReleaseAsset, error) {
				return []ReleaseAsset{{ID: 1, Name: "art1"}, {ID: 2, Name: "other"}}, nil
			},
			deleteFn: func(id int64) error { deleted = append(deleted, id); return nil },
			uploadFn: func(string, int64, string, io.Reader, string, int64) (UploadArtifactResponse, error) {
				return UploadArtifactResponse{BrowserDownloadURL: "u"}, nil
			},
		}
		if _, err := NewGithubArtifactUploader(releases, true, false).UploadArtifacts(artifacts, 1, "u"); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(deleted, []int64{1}) {
			t.Fatalf("replacesArtifacts deleted = %v", deleted)
		}
		deleted = nil
		releases = &cpReleases{
			listAssetsFn: func(int64) ([]ReleaseAsset, error) {
				return []ReleaseAsset{{ID: 1, Name: "art1"}, {ID: 2, Name: "other"}}, nil
			},
			deleteFn: func(id int64) error { deleted = append(deleted, id); return nil },
		}
		if err := NewGithubArtifactDestroyer(releases).DestroyArtifacts(1); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(deleted, []int64{1, 2}) {
			t.Fatalf("removeArtifacts deleted = %v", deleted)
		}
	})

	t.Run("CONTRACT-020", func(t *testing.T) {
		artifacts := []Artifact{NewArtifact(cpWriteFile(t, "art1", "x"), "raw")}
		attempts := 0
		releases := &cpReleases{uploadFn: func(string, int64, string, io.Reader, string, int64) (UploadArtifactResponse, error) {
			attempts++
			return UploadArtifactResponse{}, cpUploadError{message: "HTTP 500", status: 500}
		}}
		result, err := NewGithubArtifactUploader(releases, false, false).UploadArtifacts(artifacts, 1, "u")
		if err != nil || len(result) != 0 || attempts != 4 {
			t.Fatalf("result=%v err=%v attempts=%d", result, err, attempts)
		}
		attempts = 0
		releases = &cpReleases{uploadFn: func(string, int64, string, io.Reader, string, int64) (UploadArtifactResponse, error) {
			attempts++
			return UploadArtifactResponse{}, cpUploadError{message: "HTTP 500", status: 500}
		}}
		_, err = NewGithubArtifactUploader(releases, false, true).UploadArtifacts(artifacts, 1, "u")
		if err == nil || attempts != 4 {
			t.Fatalf("err=%v attempts=%d", err, attempts)
		}
	})

	t.Run("CONTRACT-021", func(t *testing.T) {
		artifacts := []Artifact{
			NewArtifact(cpWriteFile(t, "art1", "x"), "raw"),
			NewArtifact(cpWriteFile(t, "art2", "x"), "raw"),
		}
		releases := &cpReleases{uploadFn: func(_ string, _ int64, _ string, _ io.Reader, name string, _ int64) (UploadArtifactResponse, error) {
			if name == "art1" {
				return UploadArtifactResponse{}, cpUploadError{message: "bad request", status: 400}
			}
			return UploadArtifactResponse{BrowserDownloadURL: "url/" + name}, nil
		}}
		result, err := NewGithubArtifactUploader(releases, false, false).UploadArtifacts(artifacts, 1, "u")
		if err != nil {
			t.Fatal(err)
		}
		if _, ok := result["art1"]; ok {
			t.Fatalf("skipped artifact present: %v", result)
		}
		if result["art2"] != "url/art2" {
			t.Fatalf("result = %v", result)
		}
	})

	t.Run("CONTRACT-022", func(t *testing.T) {
		got := map[string]string{}
		outputs := NewCoreOutputsWith(func(name, value string) { got[name] = value })
		outputs.ApplyReleaseData(ReleaseData{ID: 42, HTMLURL: "h", UploadURL: "u", TarballURL: nil, ZipballURL: nil})
		if got["id"] != "42" || got["tarball_url"] != "" || got["zipball_url"] != "" {
			t.Fatalf("release outputs = %v", got)
		}
		outputs.ApplyAssetUrls(map[string]string{"a<b>&c": "https://x/y"})
		if got["assets"] != `{"a<b>&c":"https://x/y"}` {
			t.Fatalf("assets = %q", got["assets"])
		}
	})

	t.Run("CONTRACT-023", func(t *testing.T) {
		globber := NewFileArtifactGlobber()
		getter := cpInputGetter{values: map[string]string{"token": "t", "tag": "v1"}}
		inputs, err := LoadInputs(getter, globber, ActionContext{})
		if err != nil {
			t.Fatal(err)
		}
		if inputs.CreatedReleaseName == nil || *inputs.CreatedReleaseName != "v1" {
			t.Fatalf("name = %v", inputs.CreatedReleaseName)
		}
		if inputs.CreatedReleaseBody == nil || *inputs.CreatedReleaseBody != "" {
			t.Fatalf("empty body = %v", inputs.CreatedReleaseBody)
		}
		getter = cpInputGetter{values: map[string]string{"token": "t", "tag": "v1", "body": "B"}}
		inputs, err = LoadInputs(getter, globber, ActionContext{})
		if err != nil {
			t.Fatal(err)
		}
		if inputs.CreatedReleaseBody == nil || *inputs.CreatedReleaseBody != "B" {
			t.Fatalf("body = %v", inputs.CreatedReleaseBody)
		}
		bodyFile := cpWriteFile(t, "body.md", "from-file")
		getter = cpInputGetter{values: map[string]string{"token": "t", "tag": "v1", "bodyFile": bodyFile}}
		inputs, err = LoadInputs(getter, globber, ActionContext{})
		if err != nil {
			t.Fatal(err)
		}
		if inputs.CreatedReleaseBody == nil || *inputs.CreatedReleaseBody != "from-file" {
			t.Fatalf("bodyFile = %v", inputs.CreatedReleaseBody)
		}
	})

	t.Run("CONTRACT-024", func(t *testing.T) {
		var bodies []*string
		genNotesCalls := 0
		releases := &cpReleases{
			genNotesFn: func(string, *string, *string) (GenerateReleaseNotesResponse, error) {
				genNotesCalls++
				return GenerateReleaseNotesResponse{Body: "N"}, nil
			},
			createFn: func(_ string, body *string, _ *string, _ *string, _ *bool, _ *string, _ *string, _ *bool) (ReleaseData, error) {
				bodies = append(bodies, body)
				return ReleaseData{ID: 1}, nil
			},
		}
		inputs := &Inputs{Tag: "v1", CreatedReleaseName: cpStr("v1"), CreatedReleaseBody: cpStr("B"), GenerateReleaseNotes: true}
		if err := NewAction(inputs, &cpOutputs{}, releases, &cpUploader{}, &cpDestroyer{}, &cpSkipper{}).Perform(); err != nil {
			t.Fatal(err)
		}
		if len(bodies) != 1 || *bodies[0] != "B\nN" {
			t.Fatalf("combined body = %v", bodies)
		}
		inputs = &Inputs{Tag: "v1", CreatedReleaseName: cpStr("v1"), CreatedReleaseBody: nil, GenerateReleaseNotes: true}
		if err := NewAction(inputs, &cpOutputs{}, releases, &cpUploader{}, &cpDestroyer{}, &cpSkipper{}).Perform(); err != nil {
			t.Fatal(err)
		}
		if *bodies[1] != "N" {
			t.Fatalf("empty body = %q", *bodies[1])
		}
		genNotesCalls = 0
		inputs = &Inputs{Tag: "v1", AllowUpdates: true, UpdatedReleaseName: cpStr("v1"), UpdatedReleaseBody: cpStr("B"), GenerateReleaseNotes: true, OmitBodyDuringUpdate: true}
		releases = &cpReleases{
			genNotesFn: func(string, *string, *string) (GenerateReleaseNotesResponse, error) {
				genNotesCalls++
				return GenerateReleaseNotesResponse{Body: "N"}, nil
			},
			getByTagFn: func(string) (*ReleaseData, error) { return &ReleaseData{ID: 2, Draft: true}, nil },
			updateFn:   func(int64, string, *string, *string, *string, *bool, *string, *string, *bool) (ReleaseData, error) { return ReleaseData{ID: 2}, nil },
		}
		if err := NewAction(inputs, &cpOutputs{}, releases, &cpUploader{}, &cpDestroyer{}, &cpSkipper{}).Perform(); err != nil {
			t.Fatal(err)
		}
		if genNotesCalls != 0 {
			t.Fatalf("generate notes called %d times during omit update", genNotesCalls)
		}
	})

	t.Run("CONTRACT-025", func(t *testing.T) {
		globber := NewFileArtifactGlobber()
		getter := cpInputGetter{values: map[string]string{
			"token": "t", "tag": "v1", "name": "N", "body": "B", "draft": "true", "prerelease": "true",
			"omitBody": "true", "omitName": "true", "omitBodyDuringUpdate": "true", "omitNameDuringUpdate": "true",
			"omitDraftDuringUpdate": "true", "omitPrereleaseDuringUpdate": "true",
		}}
		inputs, err := LoadInputs(getter, globber, ActionContext{})
		if err != nil {
			t.Fatal(err)
		}
		if inputs.CreatedReleaseBody != nil || inputs.CreatedReleaseName != nil {
			t.Fatalf("created omit: body=%v name=%v", inputs.CreatedReleaseBody, inputs.CreatedReleaseName)
		}
		if inputs.UpdatedReleaseBody != nil || inputs.UpdatedReleaseName != nil || inputs.UpdatedDraft != nil || inputs.UpdatedPrerelease != nil {
			t.Fatalf("updated omit: %+v", inputs)
		}
		getter = cpInputGetter{values: map[string]string{"token": "t", "tag": "v1", "name": "N", "body": "B", "draft": "true", "prerelease": "true"}}
		inputs, err = LoadInputs(getter, globber, ActionContext{})
		if err != nil {
			t.Fatal(err)
		}
		if inputs.UpdatedReleaseBody == nil || *inputs.UpdatedReleaseBody != "B" || inputs.UpdatedDraft == nil || !*inputs.UpdatedDraft {
			t.Fatalf("no omit: %+v", inputs)
		}
	})

	t.Run("CONTRACT-026", func(t *testing.T) {
		var createdDraft *bool
		var updatedBody *string
		var updatedCommit *string
		var updatedDraft *bool
		releases := &cpReleases{
			createFn: func(_ string, _ *string, _ *string, _ *string, draft *bool, _ *string, _ *string, _ *bool) (ReleaseData, error) {
				createdDraft = draft
				return ReleaseData{ID: 9, HTMLURL: "draft"}, nil
			},
			updateFn: func(_ int64, _ string, body *string, commit *string, _ *string, draft *bool, _ *string, _ *string, _ *bool) (ReleaseData, error) {
				updatedBody = body
				updatedCommit = commit
				updatedDraft = draft
				return ReleaseData{ID: 9, HTMLURL: "published"}, nil
			},
		}
		outputs := &cpOutputs{}
		inputs := &Inputs{Tag: "v1", ImmutableCreate: true, CreatedDraft: false, CreatedReleaseName: cpStr("v1"), CreatedReleaseBody: cpStr("B"), Commit: cpStr("c")}
		if err := NewAction(inputs, outputs, releases, &cpUploader{}, &cpDestroyer{}, &cpSkipper{}).Perform(); err != nil {
			t.Fatal(err)
		}
		if createdDraft == nil || !*createdDraft {
			t.Fatalf("created draft = %v", createdDraft)
		}
		if updatedBody != nil || updatedCommit != nil || updatedDraft == nil || *updatedDraft {
			t.Fatalf("publish update body=%v commit=%v draft=%v", updatedBody, updatedCommit, updatedDraft)
		}
		if outputs.release == nil || outputs.release.HTMLURL != "published" {
			t.Fatalf("outputs = %+v", outputs.release)
		}
	})

	t.Run("CONTRACT-027", func(t *testing.T) {
		status := 404
		message := "Not Found"
		resource := "Release"
		field := "tag_name"
		err := &GithubError{Status: status, Message: message, Errors: []GithubErrorDetail{{Code: "missing", Resource: resource}}}
		if err.Status != status {
			t.Fatalf("status = %d", err.Status)
		}
		lines := strings.Split(err.String(), "\n")
		if lines[0] != fmt.Sprintf("Error %d: %s", status, message) {
			t.Fatalf("prefix = %q", lines[0])
		}
		if lines[1] != "Errors:" {
			t.Fatalf("errors header = %q", lines[1])
		}
		if !strings.HasPrefix(lines[2], "- ") || !strings.Contains(lines[2], resource) {
			t.Fatalf("bullet = %q", lines[2])
		}
		if len(lines) != 4 {
			t.Fatalf("line count = %d", len(lines))
		}

		plainMessage := "Bad"
		plain := &GithubError{Status: 400, Message: plainMessage}
		if plain.Status != 400 || plain.String() != fmt.Sprintf("Error %d: %s", 400, plainMessage) {
			t.Fatalf("plain = %q", plain.String())
		}
		if strings.Count(plain.String(), "\n") != 0 {
			t.Fatalf("plain lines = %q", plain.String())
		}

		details := []GithubErrorDetail{
			{Code: "missing_field", Resource: resource, Field: field},
			{Code: "invalid", Resource: resource, Field: field},
			{Code: "already_exists", Resource: resource},
		}
		seen := map[string]bool{}
		for _, detail := range details {
			text := detail.String()
			if text == "" || !strings.Contains(text, resource) {
				t.Fatalf("%s = %q", detail.Code, text)
			}
			if seen[text] {
				t.Fatalf("duplicate detail %q", text)
			}
			seen[text] = true
		}
		if !strings.Contains(details[0].String(), field) || !strings.Contains(details[1].String(), field) {
			t.Fatalf("field not parameterized: %q %q", details[0].String(), details[1].String())
		}
		if strings.Contains(details[2].String(), field) {
			t.Fatalf("already_exists should not use field: %q", details[2].String())
		}

		customMessage := "Custom"
		docs := "https://docs"
		custom := GithubErrorDetail{Code: "custom", Message: customMessage}
		customWithDocs := GithubErrorDetail{Code: "custom", Message: customMessage, DocumentationURL: docs}
		if custom.String() != customMessage {
			t.Fatalf("custom = %q", custom.String())
		}
		if !strings.Contains(customWithDocs.String(), docs) || len(customWithDocs.String()) <= len(custom.String()) {
			t.Fatalf("custom doc = %q", customWithDocs.String())
		}
	})

	t.Run("CONTRACT-028", func(t *testing.T) {
		var _ Releases = (*GithubReleases)(nil)
		releasesType := reflect.TypeOf((*Releases)(nil)).Elem()
		got := make([]string, 0, releasesType.NumMethod())
		for i := 0; i < releasesType.NumMethod(); i++ {
			got = append(got, releasesType.Method(i).Name)
		}
		want := []string{"Create", "DeleteArtifact", "GenerateReleaseNotes", "GetByTag", "ListArtifactsForRelease", "ListReleases", "Update", "UploadArtifact"}
		if !cpSortedEqual(got, want) {
			t.Fatalf("Releases methods = %v", got)
		}
	})

	t.Run("CONTRACT-029", func(t *testing.T) {
		normalized := cpNormalizeActionYml(cpReadActionYML(t))
		sum := sha256.Sum256([]byte(normalized))
		const want = "029ef1c67cf10d843469053175471e3a7e91f6cbcc50c35cf1cd2ed7e55dc9df"
		if hex.EncodeToString(sum[:]) != want {
			t.Fatalf("action.yml normalized hash = %s", hex.EncodeToString(sum[:]))
		}
	})

	t.Run("CONTRACT-030", func(t *testing.T) {
		files, err := filepath.Glob("*_test.go")
		if err != nil {
			t.Fatal(err)
		}
		skipPattern := regexp.MustCompile(`t\.(Skip|SkipNow|Skipf)\s*\(`)
		hardenPattern := regexp.MustCompile(`t\.(SkipIfMissingEnv|SkipOnShort)\s*\(`)
		testCount := 0
		skipCount := 0
		for _, file := range files {
			data, err := os.ReadFile(file)
			if err != nil {
				t.Fatal(err)
			}
			testCount += len(regexp.MustCompile(`(?m)^func Test`).FindAll(data, -1))
			skipCount += len(skipPattern.FindAll(data, -1))
			skipCount += len(hardenPattern.FindAll(data, -1))
		}
		if testCount == 0 {
			t.Fatal("no Test functions found among migrated *_test.go files")
		}
		if skipCount != 0 {
			t.Fatalf("found %d skipped/weakened assertions", skipCount)
		}
	})

	t.Run("CONTRACT-031", func(t *testing.T) {
		text := cpReadActionYML(t)
		inputNames := cpTopLevelKeys(text, "inputs")
		values := map[string]string{
			"token": "t", "tag": "v1", "skipIfReleaseExists": "false", "makeLatest": "legacy",
		}
		for _, name := range inputNames {
			if _, ok := values[name]; !ok {
				values[name] = ""
			}
		}
		globber := NewFileArtifactGlobber()
		inputs, err := LoadInputs(cpInputGetter{values: values}, globber, ActionContext{Owner: "o", Repo: "r"})
		if err != nil {
			t.Fatalf("LoadInputs rejected declared inputs: %v", err)
		}
		_ = inputs
		got := map[string]string{}
		outputs := NewCoreOutputsWith(func(name, value string) { got[name] = value })
		outputs.ApplyReleaseData(ReleaseData{ID: 1, HTMLURL: "h", UploadURL: "u"})
		outputs.ApplyAssetUrls(map[string]string{})
		outputNames := make([]string, 0, len(got))
		for name := range got {
			outputNames = append(outputNames, name)
		}
		if !cpSortedEqual(outputNames, cpTopLevelKeys(text, "outputs")) {
			t.Fatalf("output names = %v", outputNames)
		}
	})
}

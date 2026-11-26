// Historical internal-interface fixture; no active probe installs this file.
// All active assertions are public behavior scenarios in shared/integration/public_behavior.py.
package main

// F2P probe suite for BATCH-F2P-0003.
//
// Every subtest encodes one fail-to-pass postcondition from the batch: the Go
// target must satisfy it, while the pre-migration TypeScript source does not
// provide the target-technology path at all. The dispatcher runs this file
// against /input/target (expect PASS) and against /input/source (expect FAIL,
// because no Go implementation of the predicate exists there).
//
// All subtests are named after their observation ID and are selected with
// `go test -run 'TestProbeF2P/<OBS>'`. No network, no privilege, no unbounded
// wait: every external command is wrapped by the dispatcher.

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// Probe-local helpers (kept independent from any repository test suite).
// ---------------------------------------------------------------------------

type probeInputGetter struct {
	values map[string]string
}

func (g probeInputGetter) GetInput(name string) string { return g.values[name] }

func (g probeInputGetter) GetBooleanInput(name string) (bool, error) {
	switch strings.TrimSpace(g.values[name]) {
	case "true", "True", "TRUE":
		return true, nil
	case "false", "False", "FALSE":
		return false, nil
	}
	return false, errors.New("probe: not a YAML core schema boolean")
}

type probeGlobber struct {
	result []Artifact
	err    error
	calls  [][3]string
}

func (g *probeGlobber) GlobArtifactString(artifact string, contentType string, errorsFailBuild bool) ([]Artifact, error) {
	flag := "false"
	if errorsFailBuild {
		flag = "true"
	}
	g.calls = append(g.calls, [3]string{artifact, contentType, flag})
	return g.result, g.err
}

type probeReleases struct {
	createFn     func(tag string, body *string, commit *string, discussion *string, draft *bool, makeLatest *string, name *string, prerelease *bool) (ReleaseData, error)
	updateFn     func(id int64, tag string, body *string, commit *string, discussion *string, draft *bool, makeLatest *string, name *string, prerelease *bool) (ReleaseData, error)
	getByTagFn   func(tag string) (*ReleaseData, error)
	genNotesFn   func(tag string, previous *string, target *string) (GenerateReleaseNotesResponse, error)
	listFn       func() (*ListReleasesResponse, error)
	listAssetsFn func(releaseID int64) ([]ReleaseAsset, error)
	deleteFn     func(assetID int64) error
	uploadFn     func(assetURL string, length int64, contentType string, file io.Reader, name string, releaseID int64) (UploadArtifactResponse, error)
}

func (f *probeReleases) Create(tag string, body *string, commit *string, discussion *string, draft *bool, makeLatest *string, name *string, prerelease *bool) (ReleaseData, error) {
	if f.createFn != nil {
		return f.createFn(tag, body, commit, discussion, draft, makeLatest, name, prerelease)
	}
	return ReleaseData{}, nil
}

func (f *probeReleases) Update(id int64, tag string, body *string, commit *string, discussion *string, draft *bool, makeLatest *string, name *string, prerelease *bool) (ReleaseData, error) {
	if f.updateFn != nil {
		return f.updateFn(id, tag, body, commit, discussion, draft, makeLatest, name, prerelease)
	}
	return ReleaseData{}, nil
}

func (f *probeReleases) GetByTag(tag string) (*ReleaseData, error) {
	if f.getByTagFn != nil {
		return f.getByTagFn(tag)
	}
	return nil, nil
}

func (f *probeReleases) GenerateReleaseNotes(tag string, previous *string, target *string) (GenerateReleaseNotesResponse, error) {
	if f.genNotesFn != nil {
		return f.genNotesFn(tag, previous, target)
	}
	return GenerateReleaseNotesResponse{}, nil
}

func (f *probeReleases) ListReleases() (*ListReleasesResponse, error) {
	if f.listFn != nil {
		return f.listFn()
	}
	empty := []ReleaseData{}
	return &ListReleasesResponse{Data: &empty}, nil
}

func (f *probeReleases) ListArtifactsForRelease(releaseID int64) ([]ReleaseAsset, error) {
	if f.listAssetsFn != nil {
		return f.listAssetsFn(releaseID)
	}
	return []ReleaseAsset{}, nil
}

func (f *probeReleases) DeleteArtifact(assetID int64) error {
	if f.deleteFn != nil {
		return f.deleteFn(assetID)
	}
	return nil
}

func (f *probeReleases) UploadArtifact(assetURL string, length int64, contentType string, file io.Reader, name string, releaseID int64) (UploadArtifactResponse, error) {
	if f.uploadFn != nil {
		return f.uploadFn(assetURL, length, contentType, file, name, releaseID)
	}
	return UploadArtifactResponse{}, nil
}

type probeStatusError struct {
	status int
}

func (e probeStatusError) Error() string   { return "probe status error" }
func (e probeStatusError) StatusCode() int { return e.status }

func probeStr(value string) *string { return &value }
func probeBool(value bool) *bool    { return &value }

func probeLoadInputs(t *testing.T, values map[string]string, globber ArtifactGlobber) *Inputs {
	t.Helper()
	if globber == nil {
		globber = &probeGlobber{}
	}
	merged := map[string]string{"token": "token", "skipIfReleaseExists": "false"}
	for key, value := range values {
		merged[key] = value
	}
	inputs, err := LoadInputs(probeInputGetter{values: merged}, globber, ActionContext{
		Ref:   "refs/tags/v1.0.0",
		Owner: "ctx-owner",
		Repo:  "ctx-repo",
	})
	if err != nil {
		t.Fatalf("LoadInputs: %v", err)
	}
	return inputs
}

func probeCaptureOutputs() (Outputs, *map[string]string) {
	outputs := map[string]string{}
	return NewCoreOutputsWith(func(name, value string) { outputs[name] = value }), &outputs
}

func probeTempArtifacts(t *testing.T, contents map[string]string) []Artifact {
	t.Helper()
	dir := t.TempDir()
	artifacts := make([]Artifact, 0, len(contents))
	for name, content := range contents {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
		artifacts = append(artifacts, NewArtifact(path, "raw"))
	}
	return artifacts
}

// ---------------------------------------------------------------------------
// Observation probes.
// ---------------------------------------------------------------------------

func TestProbeF2P(t *testing.T) {
	t.Run("F2P-049", func(t *testing.T) {
		// allowUpdates is true only for the exact string "true".
		if !probeLoadInputs(t, map[string]string{"allowUpdates": "true"}, nil).AllowUpdates {
			t.Fatal("allowUpdates=true must be parsed as true")
		}
		if probeLoadInputs(t, map[string]string{"allowUpdates": "TRUE"}, nil).AllowUpdates {
			t.Fatal("allowUpdates=TRUE must not be parsed as true")
		}
	})

	t.Run("F2P-050", func(t *testing.T) {
		// Null tarball/zipball become empty strings; id stays decimal.
		outputs, captured := probeCaptureOutputs()
		outputs.ApplyReleaseData(ReleaseData{ID: 42, TarballURL: nil, ZipballURL: nil})
		if (*captured)["id"] != "42" {
			t.Fatalf("id = %q", (*captured)["id"])
		}
		if (*captured)["tarball_url"] != "" || (*captured)["zipball_url"] != "" {
			t.Fatalf("null ball urls must be empty: %+v", *captured)
		}
	})

	t.Run("F2P-051", func(t *testing.T) {
		// Generated notes are appended with a newline, or used when body empty.
		action := &Action{
			inputs: &Inputs{GenerateReleaseNotes: true},
			releases: &probeReleases{genNotesFn: func(string, *string, *string) (GenerateReleaseNotesResponse, error) {
				return GenerateReleaseNotesResponse{Body: "N"}, nil
			}},
		}
		combined, err := action.combineBodyWithReleaseNotes(probeStr("B"), false)
		if err != nil || combined == nil || *combined != "B\nN" {
			t.Fatalf("combined = %v err = %v", combined, err)
		}
		only, err := action.combineBodyWithReleaseNotes(probeStr(""), false)
		if err != nil || only == nil || *only != "N" {
			t.Fatalf("empty body = %v err = %v", only, err)
		}

		called := false
		updateAction := &Action{
			inputs: &Inputs{GenerateReleaseNotes: true, OmitBodyDuringUpdate: true},
			releases: &probeReleases{genNotesFn: func(string, *string, *string) (GenerateReleaseNotesResponse, error) {
				called = true
				return GenerateReleaseNotesResponse{Body: "N"}, nil
			}},
		}
		if _, err := updateAction.combineBodyWithReleaseNotes(probeStr("B"), true); err != nil {
			t.Fatal(err)
		}
		if called {
			t.Fatal("generate notes must not run when omitBodyDuringUpdate is true")
		}
	})

	t.Run("F2P-052", func(t *testing.T) {
		// assets output is a JSON object mapping names to download URLs.
		outputs, captured := probeCaptureOutputs()
		urls := map[string]string{"a.bin": "https://example.test/a.bin", "b.bin": "https://example.test/b.bin"}
		outputs.ApplyAssetUrls(urls)
		decoded := map[string]string{}
		if err := json.Unmarshal([]byte((*captured)["assets"]), &decoded); err != nil {
			t.Fatalf("assets is not JSON: %v", err)
		}
		if !reflect.DeepEqual(decoded, urls) {
			t.Fatalf("assets = %v", decoded)
		}
	})

	t.Run("F2P-053", func(t *testing.T) {
		// Uploading artifacts returns their download URLs.
		artifacts := probeTempArtifacts(t, map[string]string{"art1": "AAAA", "art2": "BBBB"})
		releases := &probeReleases{uploadFn: func(_ string, _ int64, _ string, _ io.Reader, name string, _ int64) (UploadArtifactResponse, error) {
			return UploadArtifactResponse{BrowserDownloadURL: "https://example.test/" + name}, nil
		}}
		uploader := NewGithubArtifactUploader(releases, false, false)
		result, err := uploader.UploadArtifacts(artifacts, 1, "https://uploads.example.test")
		if err != nil {
			t.Fatal(err)
		}
		if len(result) != 2 || result["art1"] != "https://example.test/art1" {
			t.Fatalf("result = %v", result)
		}
	})

	t.Run("F2P-054", func(t *testing.T) {
		// Published releases must skip; lookup failures must continue.
		failed := NewReleaseActionSkipper(true, &probeReleases{getByTagFn: func(string) (*ReleaseData, error) {
			return nil, errors.New("lookup failed")
		}}, "tag")
		if skip, err := failed.ShouldSkip(); err != nil || skip {
			t.Fatalf("lookup failure: skip=%v err=%v", skip, err)
		}
		published := NewReleaseActionSkipper(true, &probeReleases{getByTagFn: func(string) (*ReleaseData, error) {
			return &ReleaseData{ID: 1, Draft: false}, nil
		}}, "tag")
		if skip, err := published.ShouldSkip(); err != nil || !skip {
			t.Fatalf("published: skip=%v err=%v", skip, err)
		}
	})

	t.Run("F2P-055", func(t *testing.T) {
		// replacesArtifacts deletes only matching assets; removeArtifacts deletes all.
		artifacts := probeTempArtifacts(t, map[string]string{"art1": "AAAA"})
		deleted := []int64{}
		releases := &probeReleases{
			listAssetsFn: func(int64) ([]ReleaseAsset, error) {
				return []ReleaseAsset{{Name: "art1", ID: 1}, {Name: "other", ID: 2}}, nil
			},
			deleteFn: func(id int64) error { deleted = append(deleted, id); return nil },
			uploadFn: func(_ string, _ int64, _ string, _ io.Reader, name string, _ int64) (UploadArtifactResponse, error) {
				return UploadArtifactResponse{BrowserDownloadURL: "url/" + name}, nil
			},
		}
		uploader := NewGithubArtifactUploader(releases, true, false)
		if _, err := uploader.UploadArtifacts(artifacts, 1, "https://uploads.example.test"); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(deleted, []int64{1}) {
			t.Fatalf("replacesArtifacts deleted = %v", deleted)
		}

		destroyed := []int64{}
		destroyer := NewGithubArtifactDestroyer(&probeReleases{
			listAssetsFn: func(int64) ([]ReleaseAsset, error) {
				return []ReleaseAsset{{Name: "art1", ID: 1}, {Name: "other", ID: 2}}, nil
			},
			deleteFn: func(id int64) error { destroyed = append(destroyed, id); return nil },
		})
		if err := destroyer.DestroyArtifacts(1); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(destroyed, []int64{1, 2}) {
			t.Fatalf("removeArtifacts deleted = %v", destroyed)
		}
	})

	t.Run("F2P-056", func(t *testing.T) {
		// Empty glob warns (no error) unless artifactErrorsFailBuild is set.
		if _, err := NewArtifactPathValidator(false, []string{}, "pattern").Validate(); err != nil {
			t.Fatalf("warn mode must not fail: %v", err)
		}
		if _, err := NewArtifactPathValidator(true, []string{}, "pattern").Validate(); err == nil {
			t.Fatal("fail mode must error on empty results")
		}
	})

	t.Run("F2P-057", func(t *testing.T) {
		// Name is the base file name and content length is the file size.
		artifacts := probeTempArtifacts(t, map[string]string{"artifact": "hello world!"})
		if artifacts[0].Name != "artifact" {
			t.Fatalf("name = %q", artifacts[0].Name)
		}
		length, err := artifacts[0].ContentLength()
		if err != nil || length != 12 {
			t.Fatalf("length = %d err = %v", length, err)
		}
	})

	t.Run("F2P-058", func(t *testing.T) {
		// allowUpdates=false creates a release.
		created := false
		action := &Action{
			inputs:   &Inputs{Tag: "v1", CreatedReleaseName: probeStr("v1"), CreatedReleaseBody: probeStr("")},
			outputs:  func() Outputs { o, _ := probeCaptureOutputs(); return o }(),
			releases: &probeReleases{createFn: func(string, *string, *string, *string, *bool, *string, *string, *bool) (ReleaseData, error) {
				created = true
				return ReleaseData{ID: 1}, nil
			}},
			uploader:          NewGithubArtifactUploader(&probeReleases{}, false, false),
			artifactDestroyer: NewGithubArtifactDestroyer(&probeReleases{}),
			skipper:           NewReleaseActionSkipper(false, &probeReleases{}, "v1"),
		}
		if err := action.Perform(); err != nil {
			t.Fatal(err)
		}
		if !created {
			t.Fatal("expected Create to be called")
		}
	})

	t.Run("F2P-059", func(t *testing.T) {
		// A permanently-500 upload is attempted four times then skipped.
		artifacts := probeTempArtifacts(t, map[string]string{"art1": "AAAA"})
		attempts := 0
		releases := &probeReleases{uploadFn: func(_ string, _ int64, _ string, _ io.Reader, _ string, _ int64) (UploadArtifactResponse, error) {
			attempts++
			return UploadArtifactResponse{}, probeStatusError{status: 500}
		}}
		result, err := NewGithubArtifactUploader(releases, false, false).UploadArtifacts(artifacts, 1, "https://uploads.example.test")
		if err != nil {
			t.Fatal(err)
		}
		if attempts != 4 {
			t.Fatalf("attempts = %d, want 4", attempts)
		}
		if len(result) != 0 {
			t.Fatalf("skipped artifact must not be in assets: %v", result)
		}

		badRequestAttempts := 0
		releases400 := &probeReleases{uploadFn: func(_ string, _ int64, _ string, _ io.Reader, _ string, _ int64) (UploadArtifactResponse, error) {
			badRequestAttempts++
			return UploadArtifactResponse{}, probeStatusError{status: 400}
		}}
		result, err = NewGithubArtifactUploader(releases400, false, false).UploadArtifacts(artifacts, 1, "https://uploads.example.test")
		if err != nil {
			t.Fatal(err)
		}
		if badRequestAttempts != 1 {
			t.Fatalf("400 attempts = %d, want 1", badRequestAttempts)
		}
		if len(result) != 0 {
			t.Fatalf("400 artifact must be omitted: %v", result)
		}
	})

	t.Run("F2P-061", func(t *testing.T) {
		// Empty asset map is written as the JSON object {}.
		outputs, captured := probeCaptureOutputs()
		outputs.ApplyAssetUrls(map[string]string{})
		if (*captured)["assets"] != "{}" {
			t.Fatalf("assets = %q", (*captured)["assets"])
		}
	})

	t.Run("F2P-062", func(t *testing.T) {
		// immutableCreate+draft=false: create draft, upload, publish draft=false.
		var createdDraft *bool
		var publishDraft *bool
		var publishBody *string
		var publishCommit *string
		outputsSeen := map[string]string{}
		action := &Action{
			inputs: &Inputs{
				ImmutableCreate:    true,
				CreatedDraft:       false,
				Tag:                "v1",
				CreatedReleaseName: probeStr("v1"),
				CreatedReleaseBody: probeStr("body"),
			},
			outputs: NewCoreOutputsWith(func(name, value string) { outputsSeen[name] = value }),
			releases: &probeReleases{
				createFn: func(_ string, _ *string, _ *string, _ *string, draft *bool, _ *string, _ *string, _ *bool) (ReleaseData, error) {
					createdDraft = draft
					return ReleaseData{ID: 7, UploadURL: "https://uploads.example.test"}, nil
				},
				updateFn: func(_ int64, _ string, body *string, commit *string, _ *string, draft *bool, _ *string, _ *string, _ *bool) (ReleaseData, error) {
					publishDraft = draft
					publishBody = body
					publishCommit = commit
					return ReleaseData{ID: 7, HTMLURL: "https://example.test/7"}, nil
				},
			},
			uploader:          NewGithubArtifactUploader(&probeReleases{}, false, false),
			artifactDestroyer: NewGithubArtifactDestroyer(&probeReleases{}),
			skipper:           NewReleaseActionSkipper(false, &probeReleases{}, "v1"),
		}
		if err := action.Perform(); err != nil {
			t.Fatal(err)
		}
		if createdDraft == nil || !*createdDraft {
			t.Fatalf("create draft = %v", createdDraft)
		}
		if publishDraft == nil || *publishDraft {
			t.Fatalf("publish draft = %v", publishDraft)
		}
		if publishBody != nil || publishCommit != nil {
			t.Fatalf("publish must omit body/commit: %v/%v", publishBody, publishCommit)
		}
		if outputsSeen["html_url"] != "https://example.test/7" {
			t.Fatalf("outputs must come from publish response: %+v", outputsSeen)
		}
	})

	t.Run("F2P-063", func(t *testing.T) {
		// artifacts fallback, raw content type, owner/repo defaults.
		globber := &probeGlobber{}
		probeLoadInputs(t, map[string]string{"artifact": "legacy", "artifacts": "primary"}, globber)
		if len(globber.calls) != 1 || globber.calls[0][0] != "primary" {
			t.Fatalf("artifacts fallback calls = %v", globber.calls)
		}
		globber2 := &probeGlobber{}
		inputs := probeLoadInputs(t, map[string]string{"artifacts": "a"}, globber2)
		if globber2.calls[0][1] != "raw" {
			t.Fatalf("content type = %q", globber2.calls[0][1])
		}
		if inputs.Owner != "ctx-owner" || inputs.Repo != "ctx-repo" {
			t.Fatalf("owner/repo = %q/%q", inputs.Owner, inputs.Repo)
		}
	})

	t.Run("F2P-064", func(t *testing.T) {
		// skipIfReleaseExists accepts YAML core-schema literals and rejects others.
		t.Setenv("INPUT_SKIPIFRELEASEEXISTS", "TRUE")
		if value, err := (EnvInputGetter{}).GetBooleanInput("skipIfReleaseExists"); err != nil || !value {
			t.Fatalf("TRUE: value=%v err=%v", value, err)
		}
		t.Setenv("INPUT_SKIPIFRELEASEEXISTS", "yes")
		if _, err := (EnvInputGetter{}).GetBooleanInput("skipIfReleaseExists"); err == nil {
			t.Fatal("yes must fail YAML core schema parsing")
		}
	})

	t.Run("F2P-065", func(t *testing.T) {
		// ArtifactDestroyer deletes every existing asset.
		deleted := []int64{}
		destroyer := NewGithubArtifactDestroyer(&probeReleases{
			listAssetsFn: func(int64) ([]ReleaseAsset, error) {
				return []ReleaseAsset{{Name: "a", ID: 10}, {Name: "b", ID: 11}}, nil
			},
			deleteFn: func(id int64) error { deleted = append(deleted, id); return nil },
		})
		if err := destroyer.DestroyArtifacts(1); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(deleted, []int64{10, 11}) {
			t.Fatalf("deleted = %v", deleted)
		}
	})

	t.Run("F2P-066", func(t *testing.T) {
		// The tag input is returned verbatim.
		if got := probeLoadInputs(t, map[string]string{"tag": "v9.9.9"}, nil).Tag; got != "v9.9.9" {
			t.Fatalf("tag = %q", got)
		}
	})

	t.Run("F2P-067", func(t *testing.T) {
		// Custom error details append the documentation URL.
		got := (GithubErrorDetail{Code: "custom", Message: "foo", DocumentationURL: "https://api.example.test"}).String()
		if got != "foo\nPlease see https://api.example.test." {
			t.Fatalf("got %q", got)
		}
		if (GithubErrorDetail{Code: "custom", Message: "foo"}).String() != "foo" {
			t.Fatal("custom without documentation URL must not append one")
		}
	})

	t.Run("F2P-068", func(t *testing.T) {
		// Empty body resolves to the empty string; empty artifacts resolve to none.
		inputs := probeLoadInputs(t, map[string]string{}, nil)
		if inputs.CreatedReleaseBody == nil || *inputs.CreatedReleaseBody != "" {
			t.Fatalf("body = %v", inputs.CreatedReleaseBody)
		}
		if len(inputs.Artifacts) != 0 {
			t.Fatalf("artifacts = %v", inputs.Artifacts)
		}
	})

	t.Run("F2P-069", func(t *testing.T) {
		// already_exists details render "<resource> already exists.".
		got := (GithubErrorDetail{Code: "already_exists", Resource: "release"}).String()
		if got != "release already exists." {
			t.Fatalf("got %q", got)
		}
	})

	t.Run("F2P-070", func(t *testing.T) {
		// GitHub 404 errors append the remediation line.
		err := &GithubError{Status: 404, Message: "Not Found", Errors: []GithubErrorDetail{{Code: "missing", Resource: "Release"}}}
		want := "Error 404: Not Found\nErrors:\n- Release does not exist.\nMake sure your github token has access to the repo and has permission to author releases"
		if err.String() != want {
			t.Fatalf("got %q", err.String())
		}
	})
}

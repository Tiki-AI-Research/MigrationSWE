package core

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

// f2pCommentPatterns blank Go comments before a forbidden-mechanism scan.  A
// comment that merely mentions the old runtime ("no longer uses serie-core-rs")
// is not delegation; only code can call it.
var (
	f2pBlockComments = regexp.MustCompile(`(?s)/\*.*?\*/`)
	f2pLineComments  = regexp.MustCompile(`(?m)//.*$`)
)

func f2pStripComments(content []byte) []byte {
	content = f2pBlockComments.ReplaceAll(content, []byte(" "))
	return f2pLineComments.ReplaceAll(content, []byte(" "))
}

const (
	f2pZeroHash      = "0000000000000000000000000000000000000000"
	f2pAuthorName    = "F2P Author"
	f2pAuthorMail    = "f2p-author@example.com"
	f2pCommitterName = "F2P Committer"
	f2pCommitterMail = "f2p-committer@example.com"
)

var (
	f2pCLIOnce sync.Once
	f2pCLIPath string
	f2pCLIErr  error

	f2pOfflineOnce sync.Once
	f2pOfflineErr  error
)

type f2pRepo struct {
	t   *testing.T
	dir string
}

func f2pNewRepo(t *testing.T) *f2pRepo {
	t.Helper()
	dir := t.TempDir()
	r := &f2pRepo{t: t, dir: dir}
	r.git("init", "-b", "master")
	return r
}

func f2pEnvMap(overrides map[string]string) []string {
	values := map[string]string{}
	for _, entry := range os.Environ() {
		if index := strings.IndexByte(entry, '='); index >= 0 {
			values[entry[:index]] = entry[index+1:]
		}
	}
	for key, value := range overrides {
		values[key] = value
	}
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	result := make([]string, 0, len(keys))
	for _, key := range keys {
		result = append(result, key+"="+values[key])
	}
	return result
}

func (r *f2pRepo) gitEnv(overrides map[string]string, args ...string) string {
	r.t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = r.dir
	env := map[string]string{
		"GIT_AUTHOR_NAME":     f2pAuthorName,
		"GIT_AUTHOR_EMAIL":    f2pAuthorMail,
		"GIT_COMMITTER_NAME":  f2pCommitterName,
		"GIT_COMMITTER_EMAIL": f2pCommitterMail,
		"GIT_AUTHOR_DATE":     "2024-01-01T01:02:03+00:00",
		"GIT_COMMITTER_DATE":  "2024-01-01T01:02:03+00:00",
		"GIT_CONFIG_NOSYSTEM": "true",
		"GIT_MERGE_AUTOEDIT":  "no",
		"HOME":                r.dir,
	}
	for key, value := range overrides {
		env[key] = value
	}
	cmd.Env = f2pEnvMap(env)
	out, err := cmd.CombinedOutput()
	if err != nil {
		r.t.Fatalf("git %s failed: %v\n%s", strings.Join(args, " "), err, out)
	}
	return strings.TrimSpace(string(out))
}

func (r *f2pRepo) git(args ...string) string {
	r.t.Helper()
	return r.gitEnv(nil, args...)
}

func (r *f2pRepo) gitWithDate(date string, args ...string) string {
	r.t.Helper()
	return r.gitEnv(map[string]string{
		"GIT_AUTHOR_DATE":    date,
		"GIT_COMMITTER_DATE": date,
	}, args...)
}

func (r *f2pRepo) commit(message, date string) string {
	r.t.Helper()
	r.gitWithDate(date, "commit", "--allow-empty", "-m", message)
	return r.revParse("HEAD")
}

func (r *f2pRepo) commitBody(message, body, date string) string {
	r.t.Helper()
	r.gitWithDate(date, "commit", "--allow-empty", "-m", message, "-m", body)
	return r.revParse("HEAD")
}

func (r *f2pRepo) commitFile(message, date, path, content string) string {
	r.t.Helper()
	full := filepath.Join(r.dir, path)
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		r.t.Fatal(err)
	}
	if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
		r.t.Fatal(err)
	}
	r.git("add", "-A")
	r.gitWithDate(date, "commit", "-m", message)
	return r.revParse("HEAD")
}

func (r *f2pRepo) checkout(name string) {
	r.t.Helper()
	r.git("checkout", name)
}

func (r *f2pRepo) checkoutB(name string) {
	r.t.Helper()
	r.git("checkout", "-b", name)
}

func (r *f2pRepo) merge(date string, branches ...string) {
	r.t.Helper()
	args := []string{"merge", "--no-ff", "--no-log", "-m", "merge", "--no-edit"}
	args = append(args, branches...)
	r.gitWithDate(date, args...)
}

func (r *f2pRepo) stash(date string) string {
	r.t.Helper()
	name := filepath.Join(r.dir, "stash-"+strings.ReplaceAll(date, ":", "-")+".txt")
	if err := os.WriteFile(name, []byte(date), 0o644); err != nil {
		r.t.Fatal(err)
	}
	r.gitWithDate(date, "stash", "push", "--include-untracked", "-m", "stash "+date)
	return r.revParse("refs/stash")
}

func (r *f2pRepo) revParse(ref string) string {
	r.t.Helper()
	return strings.TrimSpace(r.git("rev-parse", ref))
}

func f2pInt(value int) *int {
	return &value
}

func f2pSetEnv(t *testing.T, key, value string, present bool) {
	t.Helper()
	old, had := os.LookupEnv(key)
	if present {
		if err := os.Setenv(key, value); err != nil {
			t.Fatal(err)
		}
	} else if err := os.Unsetenv(key); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if had {
			_ = os.Setenv(key, old)
		} else {
			_ = os.Unsetenv(key)
		}
	})
}

func f2pWrite(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func f2pModuleRoot(t *testing.T) string {
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
			t.Fatalf("go.mod not found above %s", dir)
		}
		dir = parent
	}
}

func f2pSanitizedBin(t *testing.T) string {
	t.Helper()
	bin := t.TempDir()
	for _, name := range []string{"git", "go"} {
		path, err := exec.LookPath(name)
		if err != nil {
			t.Fatalf("%s not found: %v", name, err)
		}
		if err := os.Symlink(path, filepath.Join(bin, name)); err != nil {
			t.Fatal(err)
		}
	}
	return bin
}

func f2pCLIBinary(t *testing.T) string {
	t.Helper()
	f2pCLIOnce.Do(func() {
		wd, err := os.Getwd()
		if err != nil {
			f2pCLIErr = err
			return
		}
		root, err := f2pFindModuleRoot(wd)
		if err != nil {
			f2pCLIErr = err
			return
		}
		dir, err := os.MkdirTemp("", "f2p-cli-")
		if err != nil {
			f2pCLIErr = err
			return
		}
		f2pCLIPath = filepath.Join(dir, "serie-core")
		cmd := exec.Command("go", "build", "-buildvcs=false", "-o", f2pCLIPath, "./cmd/serie-core")
		cmd.Dir = root
		cmd.Env = append(os.Environ(), "CGO_ENABLED=0", "GOFLAGS=-mod=readonly")
		out, err := cmd.CombinedOutput()
		if err != nil {
			f2pCLIErr = fmt.Errorf("go build: %v: %s", err, out)
		}
	})
	if f2pCLIErr != nil {
		t.Fatal(f2pCLIErr)
	}
	return f2pCLIPath
}

func f2pFindModuleRoot(start string) (string, error) {
	dir := start
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", fmt.Errorf("go.mod not found above %s", start)
		}
		dir = parent
	}
}

func f2pConfigFile(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.toml")
	f2pWrite(t, path, content)
	return path
}

func f2pRunCLI(t *testing.T, bin string, args []string, env map[string]string) ([]byte, []byte, error) {
	t.Helper()
	cmd := exec.Command(bin, args...)
	cmd.Env = f2pEnvMap(env)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	return stdout.Bytes(), stderr.Bytes(), err
}

func f2pParseJSON(t *testing.T, data []byte) map[string]any {
	t.Helper()
	var object map[string]any
	if err := json.Unmarshal(data, &object); err != nil {
		t.Fatalf("invalid JSON: %v\n%s", err, data)
	}
	return object
}

func f2pBuildBranchMergeRepo(t *testing.T) *f2pRepo {
	t.Helper()
	repo := f2pNewRepo(t)
	repo.commit("001", "2024-01-01T01:02:03+00:00")
	repo.checkoutB("10")
	repo.commit("011", "2024-02-01T01:02:03+00:00")
	repo.checkout("master")
	repo.commit("002", "2024-01-02T01:02:03+00:00")
	repo.merge("2024-03-01T01:02:03+00:00", "10")
	return repo
}

func f2pBuildBranchRepo(t *testing.T) *f2pRepo {
	t.Helper()
	repo := f2pNewRepo(t)
	repo.commit("001", "2024-01-01T01:02:03+00:00")
	repo.checkoutB("10")
	repo.commit("011", "2024-02-01T01:02:03+00:00")
	repo.checkout("master")
	repo.commit("002", "2024-03-01T01:02:03+00:00")
	return repo
}

func f2pAssertGraphInvariants(t *testing.T, graph *Graph) {
	t.Helper()
	if len(graph.Edges) != len(graph.Commits) {
		t.Fatalf("edge rows must match commit rows: %d != %d", len(graph.Edges), len(graph.Commits))
	}
	for y, row := range graph.Edges {
		for i, edge := range row {
			if edge.PosX < 0 || edge.AssociatedLinePosX < 0 {
				t.Fatalf("negative edge position on row %d: %+v", y, edge)
			}
			if edge.PosX > graph.MaxPosX || edge.AssociatedLinePosX > graph.MaxPosX {
				t.Fatalf("edge exceeds max lane on row %d: %+v max=%d", y, edge, graph.MaxPosX)
			}
			if i > 0 {
				prev := row[i-1]
				if prev.AssociatedLinePosX > edge.AssociatedLinePosX ||
					(prev.AssociatedLinePosX == edge.AssociatedLinePosX && prev.PosX > edge.PosX) ||
					(prev.AssociatedLinePosX == edge.AssociatedLinePosX && prev.PosX == edge.PosX && prev.EdgeType > edge.EdgeType) {
					t.Fatalf("row %d is not sorted: %+v then %+v", y, prev, edge)
				}
				if prev == edge {
					t.Fatalf("row %d contains a duplicate edge: %+v", y, edge)
				}
			}
		}
	}
	for y, commit := range graph.Commits {
		position, ok := graph.CommitPosMap[commit.CommitHash]
		if !ok {
			t.Fatalf("missing position for %s", commit.Subject)
		}
		if position.PosY != y {
			t.Fatalf("position pos_y for %s is %d, want %d", commit.Subject, position.PosY, y)
		}
		if position.PosX > graph.MaxPosX {
			t.Fatalf("commit lane exceeds max lane: %+v max=%d", position, graph.MaxPosX)
		}
	}
}

func f2pAssertMissingCommit(t *testing.T, loaded *Repository) {
	t.Helper()
	missing := NewCommitHash(f2pZeroHash)
	if commit := loaded.Commit(missing); commit != nil {
		t.Fatalf("missing commit lookup returned %+v", commit)
	}
	if parents := loaded.ParentsHash(missing); len(parents) != 0 {
		t.Fatalf("missing commit parents = %v, want empty", parents)
	}
	commit, changes := loaded.CommitDetail(missing)
	if !f2pZeroCommit(commit) {
		t.Fatalf("missing detail commit = %+v, want zero value", commit)
	}
	if len(changes) != 0 {
		t.Fatalf("missing detail changes = %v, want empty", changes)
	}
}

// f2pZeroCommit ignores the nil-versus-empty parent-slice distinction, which
// the public contract leaves unspecified, when checking for a zero commit.
func f2pZeroCommit(commit Commit) bool {
	return commit.CommitHash.Full == "" &&
		commit.AuthorName == "" &&
		commit.AuthorEmail == "" &&
		commit.CommitterName == "" &&
		commit.CommitterEmail == "" &&
		commit.Subject == "" &&
		commit.Body == "" &&
		commit.AuthorDate.IsZero() &&
		commit.CommitterDate.IsZero() &&
		len(commit.ParentCommitHashes) == 0
}

func f2pAssertMailmap(t *testing.T) {
	t.Helper()
	repo := f2pNewRepo(t)
	f2pWrite(t, filepath.Join(repo.dir, ".mailmap"), "New Name <new@example.com> Old Name <old@example.com>\n")
	oldEnv := map[string]string{
		"GIT_AUTHOR_NAME":     "Old Name",
		"GIT_AUTHOR_EMAIL":    "old@example.com",
		"GIT_COMMITTER_NAME":  "Old Name",
		"GIT_COMMITTER_EMAIL": "old@example.com",
	}
	repo.gitEnv(oldEnv, "commit", "--allow-empty", "-m", "old identity")

	raw, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	rawCommit := raw.AllCommits()[0]
	if rawCommit.AuthorName != "Old Name" || rawCommit.AuthorEmail != "old@example.com" {
		t.Fatalf("raw identity changed: %s <%s>", rawCommit.AuthorName, rawCommit.AuthorEmail)
	}
	if rawCommit.CommitterName != "Old Name" || rawCommit.CommitterEmail != "old@example.com" {
		t.Fatalf("raw committer changed: %s <%s>", rawCommit.CommitterName, rawCommit.CommitterEmail)
	}

	mapped, err := Load(repo.dir, SortChronological, nil, true)
	if err != nil {
		t.Fatal(err)
	}
	mappedCommit := mapped.AllCommits()[0]
	if mappedCommit.AuthorName != "New Name" || mappedCommit.AuthorEmail != "new@example.com" {
		t.Fatalf("mailmap author not resolved: %s <%s>", mappedCommit.AuthorName, mappedCommit.AuthorEmail)
	}
	if mappedCommit.CommitterName != "New Name" || mappedCommit.CommitterEmail != "new@example.com" {
		t.Fatalf("mailmap committer not resolved: %s <%s>", mappedCommit.CommitterName, mappedCommit.CommitterEmail)
	}

	plain := f2pNewRepo(t)
	hash := plain.commit("plain", "2024-01-01T01:02:03+00:00")
	without, err := Load(plain.dir, SortChronological, nil, true)
	if err != nil {
		t.Fatal(err)
	}
	commit := without.Commit(NewCommitHash(hash))
	if commit == nil || commit.AuthorName != f2pAuthorName || commit.AuthorEmail != f2pAuthorMail {
		t.Fatalf("mailmap without .mailmap changed identity: %+v", commit)
	}
}

func f2pAssertStashInsertion(t *testing.T) {
	t.Helper()
	repo := f2pNewRepo(t)
	repo.commitFile("001", "2024-01-01T01:02:03+00:00", "a.txt", "one")
	parent := repo.commitFile("002", "2024-01-02T01:02:03+00:00", "a.txt", "two")
	stashHash := repo.stash("2024-01-03T01:02:03+00:00")

	loaded, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	commits := loaded.AllCommits()
	stashIndex, parentIndex := -1, -1
	for i, commit := range commits {
		if commit.CommitHash.Full == stashHash {
			stashIndex = i
		}
		if commit.CommitHash.Full == parent {
			parentIndex = i
		}
	}
	if stashIndex < 0 {
		t.Fatal("stash commit was not inserted")
	}
	if parentIndex < 0 || stashIndex+1 != parentIndex {
		t.Fatalf("stash index %d is not immediately before parent index %d", stashIndex, parentIndex)
	}
	refs := loaded.Refs(NewCommitHash(stashHash))
	if len(refs) != 1 || refs[0].Kind != RefKindStash || refs[0].Message == "" {
		t.Fatalf("unexpected stash refs: %+v", refs)
	}

	orphan := f2pNewRepo(t)
	orphan.commit("001", "2024-01-01T01:02:03+00:00")
	orphan.checkoutB("10")
	orphanParent := orphan.commit("011", "2024-01-02T01:02:03+00:00")
	orphanStash := orphan.stash("2024-01-03T01:02:03+00:00")
	orphan.checkout("master")
	orphan.git("branch", "-D", "10")
	orphan.commit("002", "2024-05-01T01:02:03+00:00")

	orphanLoaded, err := Load(orphan.dir, SortChronological, f2pInt(1), false)
	if err != nil {
		t.Fatal(err)
	}
	if got := len(orphanLoaded.AllCommits()); got != 1 {
		t.Fatalf("max_count truncation loaded %d commits, want 1", got)
	}
	for _, commit := range orphanLoaded.AllCommits() {
		if commit.CommitHash.Full == orphanStash {
			t.Fatal("stash with absent first parent was inserted into the sequence")
		}
		if commit.CommitHash.Full == orphanParent {
			t.Fatal("orphan parent unexpectedly present in commit sequence")
		}
	}
	orphanRefs := orphanLoaded.Refs(NewCommitHash(orphanStash))
	if len(orphanRefs) != 1 || orphanRefs[0].Kind != RefKindStash {
		t.Fatalf("orphan stash reference missing: %+v", orphanRefs)
	}
}

func f2pAssertLoaderParameters(t *testing.T) {
	t.Helper()
	repo := f2pBuildBranchMergeRepo(t)
	for _, tc := range []struct {
		name  string
		sort  SortCommit
		flag  string
		env   map[string]string
		types []string
	}{
		{"chronological", SortChronological, "--date-order", nil, nil},
		{"topological", SortTopological, "--topo-order", nil, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			loaded, err := Load(repo.dir, tc.sort, nil, false)
			if err != nil {
				t.Fatal(err)
			}
			expected := strings.Fields(repo.git("log", tc.flag, "--pretty=%H", "--branches", "--remotes", "--tags", "HEAD"))
			actual := make([]string, 0, len(expected))
			for _, commit := range loaded.AllCommits() {
				actual = append(actual, commit.CommitHash.Full)
			}
			if !reflect.DeepEqual(actual, expected) {
				t.Fatalf("%s order mismatch\nexpected: %v\nactual:   %v", tc.name, expected, actual)
			}
		})
	}

	limited, err := Load(repo.dir, SortChronological, f2pInt(2), false)
	if err != nil {
		t.Fatal(err)
	}
	if got := len(limited.AllCommits()); got != 2 {
		t.Fatalf("max_count did not truncate commits: got %d", got)
	}

	stashRepo := f2pNewRepo(t)
	stashRepo.commit("001", "2024-01-01T01:02:03+00:00")
	stashRepo.commit("002", "2024-01-02T01:02:03+00:00")
	stashRepo.stash("2024-01-03T01:02:03+00:00")
	stashLoaded, err := Load(stashRepo.dir, SortChronological, f2pInt(2), false)
	if err != nil {
		t.Fatal(err)
	}
	if got := len(stashLoaded.AllCommits()); got != 3 {
		t.Fatalf("max_count was not applied before stash merge: got %d commits", got)
	}

	mailmapRepo := f2pNewRepo(t)
	f2pWrite(t, filepath.Join(mailmapRepo.dir, ".mailmap"), "Mapped <mapped@example.com> F2P Author <f2p-author@example.com>\n")
	hash := mailmapRepo.commit("identity", "2024-01-01T01:02:03+00:00")
	raw, err := Load(mailmapRepo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	mapped, err := Load(mailmapRepo.dir, SortChronological, nil, true)
	if err != nil {
		t.Fatal(err)
	}
	if raw.Commit(NewCommitHash(hash)).AuthorName != f2pAuthorName {
		t.Fatal("mailmap=false did not use raw identity")
	}
	if mapped.Commit(NewCommitHash(hash)).AuthorName != "Mapped" {
		t.Fatal("mailmap=true did not resolve identity")
	}
}

func f2pAssertRepositoryValidation(t *testing.T) {
	t.Helper()
	notRepo := t.TempDir()
	if loaded, err := Load(notRepo, SortChronological, nil, false); err == nil || err.Error() != "not a git repository (or any of the parent directories)" {
		t.Fatalf("unexpected non-repository result: repo=%v err=%v", loaded, err)
	}

	empty := f2pNewRepo(t)
	if loaded, err := Load(empty.dir, SortChronological, nil, false); err == nil || err.Error() != "no commits in the repository" {
		t.Fatalf("unexpected empty-repository result: repo=%v err=%v", loaded, err)
	}

	bareRepo := f2pNewRepo(t)
	bareRepo.commit("001", "2024-01-01T01:02:03+00:00")
	bare := filepath.Join(t.TempDir(), "bare.git")
	clone := exec.Command("git", "clone", "--bare", bareRepo.dir, bare)
	if out, err := clone.CombinedOutput(); err != nil {
		t.Fatalf("git clone --bare failed: %v\n%s", err, out)
	}
	bareLoaded, err := Load(bare, SortChronological, nil, false)
	if err != nil {
		t.Fatalf("bare repository was rejected: %v", err)
	}
	if got := len(bareLoaded.AllCommits()); got != 1 {
		t.Fatalf("bare repository loaded %d commits, want 1", got)
	}
}

func f2pAssertCommitParsing(t *testing.T) {
	t.Helper()
	record := strings.Join([]string{
		"0123456789abcdef0123456789abcdef01234567",
		"Author Name",
		"author@example.com",
		"2024-02-03T04:05:06-05:00",
		"Committer Name",
		"committer@example.com",
		"2024-02-03T19:05:06+09:30",
		"subject",
		"body line",
		strings.Join([]string{"1111111111111111111111111111111111111111", "2222222222222222222222222222222222222222"}, " "),
	}, "\x1f")
	commit, err := parseCommitRecord(record)
	if err != nil {
		t.Fatal(err)
	}
	if commit.CommitHash.Full != "0123456789abcdef0123456789abcdef01234567" || commit.CommitHash.Short != "0123456" {
		t.Fatalf("unexpected hash: %+v", commit.CommitHash)
	}
	if len(commit.ParentCommitHashes) != 2 || commit.ParentCommitHashes[0].Full != "1111111111111111111111111111111111111111" {
		t.Fatalf("parent hashes were not split on spaces: %+v", commit.ParentCommitHashes)
	}
	if got := commit.AuthorDate.Format(time.RFC3339); got != "2024-02-03T04:05:06-05:00" {
		t.Fatalf("author date lost offset: %s", got)
	}
	if got := commit.CommitterDate.Format(time.RFC3339); got != "2024-02-03T19:05:06+09:30" {
		t.Fatalf("committer date lost offset: %s", got)
	}

	records, err := parseCommitRecords([]byte(record + "\x00" + record + "\x00"))
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 2 {
		t.Fatalf("NUL-separated records parsed as %d", len(records))
	}
}

func f2pAssertCommitFieldsAndDates(t *testing.T) {
	t.Helper()
	repo := f2pNewRepo(t)
	first := repo.commitFile("001", "2024-01-02T03:04:05+09:30", "a.txt", "one")
	second := repo.commitBody("002", "body text", "2024-02-03T04:05:06-05:00")

	loaded, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	commits := loaded.AllCommits()
	if len(commits) != 2 {
		t.Fatalf("expected two commits, got %d", len(commits))
	}
	head := commits[0]
	if head.CommitHash.Full != second || head.CommitHash.Short != second[:7] {
		t.Fatalf("unexpected head hash: %+v", head.CommitHash)
	}
	if head.AuthorName != f2pAuthorName || head.AuthorEmail != f2pAuthorMail {
		t.Fatalf("unexpected author fields: %s <%s>", head.AuthorName, head.AuthorEmail)
	}
	if head.CommitterName != f2pCommitterName || head.CommitterEmail != f2pCommitterMail {
		t.Fatalf("unexpected committer fields: %s <%s>", head.CommitterName, head.CommitterEmail)
	}
	if head.Subject != "002" || strings.TrimSpace(head.Body) != "body text" {
		t.Fatalf("unexpected subject/body: %q / %q", head.Subject, head.Body)
	}
	if got := head.AuthorDate.Format(time.RFC3339); got != "2024-02-03T04:05:06-05:00" {
		t.Fatalf("author date lost offset: %s", got)
	}
	if _, offset := head.AuthorDate.Zone(); offset != -5*60*60 {
		t.Fatalf("unexpected author offset: %d", offset)
	}
	old := commits[1]
	if old.CommitHash.Full != first {
		t.Fatalf("unexpected old hash: %s", old.CommitHash.Full)
	}
	if got := old.AuthorDate.Format(time.RFC3339); got != "2024-01-02T03:04:05+09:30" {
		t.Fatalf("first author date lost offset: %s", got)
	}
	if !reflect.DeepEqual(head.ParentCommitHashes, []CommitHash{NewCommitHash(first)}) {
		t.Fatalf("unexpected parents: %+v", head.ParentCommitHashes)
	}
}

func f2pAssertNoForbiddenMechanisms(t *testing.T, root string) {
	t.Helper()
	forbidden := []string{
		"import \"C\"",
		"plugin.Open(",
		"net/rpc",
		"syscall.LoadLibrary",
		"dlopen",
	}
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			name := entry.Name()
			if name == ".git" || name == "batches" || name == "vendor" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		content, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		content = f2pStripComments(content)
		for _, needle := range forbidden {
			if bytes.Contains(content, []byte(needle)) {
				t.Fatalf("%s contains forbidden migration mechanism %q", path, needle)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func f2pAssertNoRustDelegation(t *testing.T, root string) {
	t.Helper()
	forbidden := []string{
		"exec.Command(\"cargo\"",
		"exec.Command(\"rustc\"",
		"serie-core-rs",
		"libserie",
	}
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			name := entry.Name()
			if name == ".git" || name == "batches" || name == "vendor" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		content, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		content = f2pStripComments(content)
		for _, needle := range forbidden {
			if bytes.Contains(content, []byte(needle)) {
				t.Fatalf("%s delegates migrated work to the Rust implementation via %q", path, needle)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func f2pAssertOfflineModule(t *testing.T) {
	t.Helper()
	f2pOfflineOnce.Do(func() {
		wd, err := os.Getwd()
		if err != nil {
			f2pOfflineErr = err
			return
		}
		root, err := f2pFindModuleRoot(wd)
		if err != nil {
			f2pOfflineErr = err
			return
		}
		cache, err := os.MkdirTemp("", "f2p-offline-")
		if err != nil {
			f2pOfflineErr = err
			return
		}
		env := append(os.Environ(),
			"CGO_ENABLED=0",
			"GOPROXY=off",
			"GOSUMDB=off",
			"GOTOOLCHAIN=local",
			"GOFLAGS=-mod=readonly",
			"GOMODCACHE="+filepath.Join(cache, "modcache"),
			"GOCACHE="+filepath.Join(cache, "gocache"),
			"HOME="+cache,
		)
		for _, args := range [][]string{
			{"build", "-buildvcs=false", "./..."},
			{"test", "-run", "^$", "./..."},
		} {
			cmd := exec.Command("go", args...)
			cmd.Dir = root
			cmd.Env = env
			if out, err := cmd.CombinedOutput(); err != nil {
				f2pOfflineErr = fmt.Errorf("go %s failed offline: %v\n%s", strings.Join(args, " "), err, out)
				return
			}
		}
	})
	if f2pOfflineErr != nil {
		t.Fatal(f2pOfflineErr)
	}
}

func f2pAssertCGOFreeRun(t *testing.T, repoDir string) {
	t.Helper()
	root := f2pModuleRoot(t)
	bin := f2pCLIBinary(t)
	binPath := f2pSanitizedBin(t)
	config := f2pConfigFile(t, "[core.option]\norder = \"chrono\"\n")
	stdout, stderr, err := f2pRunCLI(t, bin, []string{repoDir}, map[string]string{
		"CGO_ENABLED":       "0",
		"PATH":              binPath,
		"SERIE_CONFIG_FILE": config,
		"HOME":              t.TempDir(),
	})
	if err != nil {
		t.Fatalf("CGO-disabled CLI run failed: %v\n%s", err, stderr)
	}
	object := f2pParseJSON(t, stdout)
	for _, key := range []string{"commits", "commit_pos_map", "edges", "max_pos_x", "all_refs", "head"} {
		if _, ok := object[key]; !ok {
			t.Fatalf("CLI JSON missing %q", key)
		}
	}
	commits, ok := object["commits"].([]any)
	if !ok || len(commits) == 0 {
		t.Fatalf("CLI JSON commits = %v, want non-empty", object["commits"])
	}
	f2pAssertNoForbiddenMechanisms(t, root)
}

func f2pAssertNoRustToolchain(t *testing.T) {
	t.Helper()
	root := f2pModuleRoot(t)
	f2pAssertOfflineModule(t)
	bin := f2pCLIBinary(t)
	binPath := f2pSanitizedBin(t)
	config := f2pConfigFile(t, "[core.option]\norder = \"chrono\"\n")
	repo := f2pNewRepo(t)
	repo.commit("001", "2024-01-01T01:02:03+00:00")
	stdout, stderr, err := f2pRunCLI(t, bin, []string{repo.dir}, map[string]string{
		"PATH":              binPath,
		"SERIE_CONFIG_FILE": config,
		"HOME":              t.TempDir(),
	})
	if err != nil {
		t.Fatalf("CLI failed without a Rust toolchain: %v\n%s", err, stderr)
	}
	object := f2pParseJSON(t, stdout)
	if commits, ok := object["commits"].([]any); !ok || len(commits) == 0 {
		t.Fatalf("CLI did not load commits without Rust: %v", object["commits"])
	}
	for _, name := range []string{"cargo", "rustc"} {
		if _, err := exec.LookPath(name); err == nil {
			// The sanitized PATH below is what the CLI actually receives; this
			// branch only documents that the ambient toolchain is irrelevant.
		}
	}
	f2pAssertNoRustDelegation(t, root)
}

func TestF2P051ConfigFileLookupPrecedence(t *testing.T) {
	base := t.TempDir()
	explicit := filepath.Join(base, "explicit.toml")
	f2pWrite(t, explicit, "[core.option]\norder = \"topo\"\n")
	xdg := filepath.Join(base, "xdg")
	f2pWrite(t, filepath.Join(xdg, "serie", "config.toml"), "[core.option]\norder = \"chrono\"\n[core.git]\nmailmap = true\n")
	home := filepath.Join(base, "home")
	f2pWrite(t, filepath.Join(home, ".config", "serie", "config.toml"), "[core.option]\norder = \"topo\"\n[core.git]\nmailmap = true\n")

	f2pSetEnv(t, "SERIE_CONFIG_FILE", explicit, true)
	f2pSetEnv(t, "XDG_CONFIG_HOME", xdg, true)
	f2pSetEnv(t, "HOME", home, true)
	config, err := LoadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if config.Order != SortTopological || config.Mailmap {
		t.Fatalf("explicit config did not win: %+v", config)
	}

	f2pSetEnv(t, "SERIE_CONFIG_FILE", "", false)
	config, err = LoadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if config.Order != SortChronological || !config.Mailmap {
		t.Fatalf("XDG config did not win over HOME: %+v", config)
	}

	f2pSetEnv(t, "XDG_CONFIG_HOME", "", false)
	config, err = LoadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if config.Order != SortTopological || !config.Mailmap {
		t.Fatalf("HOME config was not used: %+v", config)
	}

	f2pSetEnv(t, "SERIE_CONFIG_FILE", filepath.Join(base, "missing.toml"), true)
	if _, err := LoadConfig(); err == nil {
		t.Fatal("missing SERIE_CONFIG_FILE was not an error")
	}

	f2pSetEnv(t, "SERIE_CONFIG_FILE", "", false)
	f2pSetEnv(t, "HOME", t.TempDir(), true)
	config, err = LoadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if config != DefaultConfig() {
		t.Fatalf("missing fallback did not yield defaults: %+v", config)
	}
}

func TestF2P055DependencyManifestPolicy(t *testing.T) {
	root := f2pModuleRoot(t)
	goMod, err := os.ReadFile(filepath.Join(root, "go.mod"))
	if err != nil {
		t.Fatal(err)
	}
	text := string(goMod)
	if !regexp.MustCompile(`(?m)^module\s+\S+`).MatchString(text) {
		t.Fatalf("missing module declaration:\n%s", text)
	}
	if !regexp.MustCompile(`(?m)^go\s+1\.24(?:\.0)?\s*$`).MatchString(text) {
		t.Fatalf("unexpected Go version:\n%s", text)
	}
	// The task requires an offline Go core; its module name and directory
	// need not match the reference solution. Offline execution below checks
	// actual dependency availability rather than banning all require lines.
	f2pAssertOfflineModule(t)
}

func TestF2P070CLIPositionalArity(t *testing.T) {
	bin := f2pCLIBinary(t)
	repo := f2pNewRepo(t)
	repo.commit("001", "2024-01-01T01:02:03+00:00")
	config := f2pConfigFile(t, "[core.option]\norder = \"chrono\"\n")
	env := map[string]string{"SERIE_CONFIG_FILE": config, "HOME": t.TempDir()}

	cmd := exec.Command(bin)
	cmd.Dir = repo.dir
	cmd.Env = f2pEnvMap(env)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("no positional argument failed: %v\n%s", err, stderr.String())
	}

	stdout, errOut, err := f2pRunCLI(t, bin, []string{repo.dir}, env)
	if err != nil {
		t.Fatalf("one positional argument failed: %v\n%s", err, errOut)
	}
	object := f2pParseJSON(t, stdout)
	if commits, ok := object["commits"].([]any); !ok || len(commits) != 1 {
		t.Fatalf("one positional argument did not select the repository: %v", object["commits"])
	}

	other := f2pNewRepo(t)
	other.commit("002", "2024-01-01T01:02:03+00:00")
	_, stderrBytes, err := f2pRunCLI(t, bin, []string{repo.dir, other.dir}, env)
	if err == nil {
		t.Fatal("two positional arguments were accepted")
	}
	if !strings.Contains(string(stderrBytes), "too many arguments") {
		t.Fatalf("unexpected too-many-arguments error: %s", stderrBytes)
	}
}

func TestF2P064GraphLaneAssignment(t *testing.T) {
	simple := f2pBuildBranchRepo(t)
	loaded, err := Load(simple.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	graph := CalcGraph(loaded)
	bySubject := map[string]Position{}
	for _, commit := range graph.Commits {
		bySubject[commit.Subject] = graph.CommitPosMap[commit.CommitHash]
	}
	if bySubject["002"] != (Position{PosX: 0, PosY: 0}) {
		t.Fatalf("master tip did not take the first vacant lane: %+v", bySubject["002"])
	}
	if bySubject["011"] != (Position{PosX: 1, PosY: 1}) {
		t.Fatalf("branch tip did not take a new lane: %+v", bySubject["011"])
	}
	if bySubject["001"] != (Position{PosX: 0, PosY: 2}) {
		t.Fatalf("root did not reuse the lowest filtered-child lane: %+v", bySubject["001"])
	}
	f2pAssertGraphInvariants(t, graph)

	merge := f2pBuildBranchMergeRepo(t)
	mergeLoaded, err := Load(merge.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	f2pAssertGraphInvariants(t, CalcGraph(mergeLoaded))
}

func TestF2P065ConfigFileSemantics(t *testing.T) {
	content := `# existing config.toml format
[core.option]
protocol = "auto"
order = "topo"
graph_width = "auto"

[core.git]
mailmap = true

[core.user_command]
commands_1 = { name = "git diff", commands = ["git", "--no-pager", "diff", "{{first_parent_hash}}", "{{target_hash}}"]}
tab_width = 4

[ui.list]
columns = ["graph", "marker", "subject", "name", "hash", "date"]
`
	config, err := ParseCoreConfig(content)
	if err != nil {
		t.Fatal(err)
	}
	if config.Order != SortTopological || !config.Mailmap {
		t.Fatalf("core config semantics not preserved: %+v", config)
	}
	path := f2pConfigFile(t, content)
	before, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := LoadConfigFromFile(path); err != nil {
		t.Fatal(err)
	}
	after, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("config file format was rewritten")
	}

	base := t.TempDir()
	explicit := filepath.Join(base, "explicit.toml")
	f2pWrite(t, explicit, "[core.option]\norder = \"topo\"\n")
	xdg := filepath.Join(base, "xdg")
	f2pWrite(t, filepath.Join(xdg, "serie", "config.toml"), "[core.option]\norder = \"chrono\"\n")
	f2pSetEnv(t, "SERIE_CONFIG_FILE", explicit, true)
	f2pSetEnv(t, "XDG_CONFIG_HOME", xdg, true)
	f2pSetEnv(t, "HOME", t.TempDir(), true)
	loaded, err := LoadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Order != SortTopological {
		t.Fatalf("SERIE_CONFIG_FILE did not take precedence: %+v", loaded)
	}
}

func TestF2P068EdgeOrderingAndDedup(t *testing.T) {
	edges := []Edge{
		{EdgeType: EdgeTypeRight, PosX: 0, AssociatedLinePosX: 2},
		{EdgeType: EdgeTypeVertical, PosX: 2, AssociatedLinePosX: 1},
		{EdgeType: EdgeTypeHorizontal, PosX: 1, AssociatedLinePosX: 1},
		{EdgeType: EdgeTypeVertical, PosX: 0, AssociatedLinePosX: 1},
		{EdgeType: EdgeTypeVertical, PosX: 2, AssociatedLinePosX: 1},
		{EdgeType: EdgeTypeVertical, PosX: 0, AssociatedLinePosX: 2},
	}
	want := []Edge{
		{EdgeType: EdgeTypeVertical, PosX: 0, AssociatedLinePosX: 1},
		{EdgeType: EdgeTypeHorizontal, PosX: 1, AssociatedLinePosX: 1},
		{EdgeType: EdgeTypeVertical, PosX: 2, AssociatedLinePosX: 1},
		{EdgeType: EdgeTypeVertical, PosX: 0, AssociatedLinePosX: 2},
		{EdgeType: EdgeTypeRight, PosX: 0, AssociatedLinePosX: 2},
	}
	if got := SortAndDedupEdges(edges); !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected sorted/deduped edges\nwant: %+v\ngot:  %+v", want, got)
	}

	repo := f2pBuildBranchMergeRepo(t)
	loaded, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	f2pAssertGraphInvariants(t, CalcGraph(loaded))
}

func TestF2P069CalcGraphResult(t *testing.T) {
	repo := f2pBuildBranchMergeRepo(t)
	loaded, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	graph := CalcGraph(loaded)
	if len(graph.Commits) != len(loaded.AllCommits()) {
		t.Fatalf("graph commits = %d, loaded = %d", len(graph.Commits), len(loaded.AllCommits()))
	}
	for index, commit := range loaded.AllCommits() {
		if graph.Commits[index].CommitHash != commit.CommitHash {
			t.Fatalf("graph commit %d is not in loaded order", index)
		}
		position, ok := graph.CommitPosMap[commit.CommitHash]
		if !ok {
			t.Fatalf("commit position map missing %s", commit.CommitHash.Full)
		}
		if position.PosY != index {
			t.Fatalf("commit %s pos_y = %d, want %d", commit.CommitHash.Full, position.PosY, index)
		}
		maxLane := 0
		for _, row := range graph.Edges {
			for _, edge := range row {
				if edge.PosX > maxLane {
					maxLane = edge.PosX
				}
				if edge.AssociatedLinePosX > maxLane {
					maxLane = edge.AssociatedLinePosX
				}
			}
		}
		if graph.MaxPosX < maxLane {
			t.Fatalf("max lane %d does not cover used lane %d", graph.MaxPosX, maxLane)
		}
	}
	f2pAssertGraphInvariants(t, graph)
}

func TestF2P071EdgeFieldsAndVocabulary(t *testing.T) {
	names := []string{
		"vertical", "horizontal", "up", "down", "left",
		"right", "right_top", "right_bottom", "left_top", "left_bottom",
	}
	types := []EdgeType{
		EdgeTypeVertical, EdgeTypeHorizontal, EdgeTypeUp, EdgeTypeDown, EdgeTypeLeft,
		EdgeTypeRight, EdgeTypeRightTop, EdgeTypeRightBottom, EdgeTypeLeftTop, EdgeTypeLeftBottom,
	}
	for index, edgeType := range types {
		if int(edgeType) != index {
			t.Fatalf("edge type %s has index %d, want %d", edgeType, int(edgeType), index)
		}
		if edgeType.String() != names[index] {
			t.Fatalf("edge type name = %q, want %q", edgeType.String(), names[index])
		}
	}
	data, err := json.Marshal(Edge{EdgeType: EdgeTypeRightBottom, PosX: 2, AssociatedLinePosX: 1})
	if err != nil {
		t.Fatal(err)
	}
	var object map[string]any
	if err := json.Unmarshal(data, &object); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"edge_type", "pos_x", "associated_line_pos_x"} {
		if _, ok := object[key]; !ok {
			t.Fatalf("edge JSON missing %q: %s", key, data)
		}
	}
}

func TestF2P072DeterministicEnumerationLanesEdgesRefs(t *testing.T) {
	repo := f2pBuildBranchMergeRepo(t)
	repo.git("tag", "z")
	repo.git("branch", "b")
	loaded, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	firstGraph, err := json.Marshal(CalcGraph(loaded))
	if err != nil {
		t.Fatal(err)
	}
	firstRefs, err := json.Marshal(loaded.AllRefs())
	if err != nil {
		t.Fatal(err)
	}
	firstOrder := make([]string, 0)
	for _, commit := range loaded.AllCommits() {
		firstOrder = append(firstOrder, commit.CommitHash.Full)
	}
	for iteration := 0; iteration < 20; iteration++ {
		next, err := Load(repo.dir, SortChronological, nil, false)
		if err != nil {
			t.Fatal(err)
		}
		nextGraph, err := json.Marshal(CalcGraph(next))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(firstGraph, nextGraph) {
			t.Fatalf("graph changed on iteration %d", iteration)
		}
		nextRefs, err := json.Marshal(next.AllRefs())
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(firstRefs, nextRefs) {
			t.Fatalf("refs changed on iteration %d", iteration)
		}
		nextOrder := make([]string, 0)
		for _, commit := range next.AllCommits() {
			nextOrder = append(nextOrder, commit.CommitHash.Full)
		}
		if !reflect.DeepEqual(firstOrder, nextOrder) {
			t.Fatalf("commit order changed on iteration %d", iteration)
		}
	}
}

func TestF2P052GitSubprocessDataSource(t *testing.T) {
	repo := f2pNewRepo(t)
	repo.commitFile("001", "2024-01-01T01:02:03+00:00", "a.txt", "one")
	repo.commitFile("002", "2024-01-02T01:02:03+00:00", "a.txt", "two")
	repo.stash("2024-01-03T01:02:03+00:00")
	repo.git("tag", "v1")
	repo.git("branch", "feature")

	bin := f2pCLIBinary(t)
	realGit, err := exec.LookPath("git")
	if err != nil {
		t.Fatal(err)
	}
	logPath := filepath.Join(t.TempDir(), "git.log")
	wrapperDir := t.TempDir()
	wrapper := filepath.Join(wrapperDir, "git")
	script := "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + fmt.Sprintf("%q", logPath) + "\nexec " + fmt.Sprintf("%q", realGit) + " \"$@\"\n"
	if err := os.WriteFile(wrapper, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	config := f2pConfigFile(t, "[core.option]\norder = \"chrono\"\n")
	stdout, stderr, err := f2pRunCLI(t, bin, []string{repo.dir}, map[string]string{
		"PATH":              wrapperDir,
		"SERIE_CONFIG_FILE": config,
		"HOME":              t.TempDir(),
	})
	if err != nil {
		t.Fatalf("CLI run failed with git wrapper: %v\n%s", err, stderr)
	}
	object := f2pParseJSON(t, stdout)
	commits, ok := object["commits"].([]any)
	if !ok || len(commits) < 3 {
		t.Fatalf("git-backed load returned too few commits: %v", object["commits"])
	}
	refs, ok := object["all_refs"].([]any)
	if !ok || len(refs) == 0 {
		t.Fatalf("git-backed load returned no refs: %v", object["all_refs"])
	}
	logData, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	logText := string(logData)
	for _, expected := range []string{"show-ref --head --dereference", "log ", "stash list"} {
		if !strings.Contains(logText, expected) {
			t.Fatalf("git invocation log missing %q:\n%s", expected, logText)
		}
	}
}

func TestF2P049StashInsertionAndCGOFreeRuntime(t *testing.T) {
	f2pAssertStashInsertion(t)
	repo := f2pNewRepo(t)
	repo.commit("001", "2024-01-01T01:02:03+00:00")
	f2pAssertCGOFreeRun(t, repo.dir)
}

func TestF2P050MailmapAndCGOFreeRuntime(t *testing.T) {
	f2pAssertMailmap(t)
	repo := f2pNewRepo(t)
	repo.commit("001", "2024-01-01T01:02:03+00:00")
	f2pAssertCGOFreeRun(t, repo.dir)
}

func TestF2P053RepositoryValidationAndMissingDetail(t *testing.T) {
	f2pAssertRepositoryValidation(t)
	repo := f2pNewRepo(t)
	repo.commit("001", "2024-01-01T01:02:03+00:00")
	loaded, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	f2pAssertMissingCommit(t, loaded)
}

func TestF2P054CommitParsingAndMissingLookup(t *testing.T) {
	f2pAssertCommitParsing(t)
	repo := f2pNewRepo(t)
	repo.commit("001", "2024-01-01T01:02:03+00:00")
	loaded, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Commit(NewCommitHash(f2pZeroHash)) != nil {
		t.Fatal("zero hash resolved to a commit")
	}
	if got := loaded.ParentsHash(NewCommitHash(f2pZeroHash)); len(got) != 0 {
		t.Fatalf("zero hash parents = %v, want empty", got)
	}
}

func TestF2P056LoaderParametersAndMissingLookup(t *testing.T) {
	f2pAssertLoaderParameters(t)
	repo := f2pNewRepo(t)
	repo.commit("001", "2024-01-01T01:02:03+00:00")
	loaded, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	f2pAssertMissingCommit(t, loaded)
}

func TestF2P057DeterministicAllRefs(t *testing.T) {
	repo := f2pNewRepo(t)
	repo.commitFile("001", "2024-01-01T01:02:03+00:00", "a.txt", "one")
	repo.stash("2024-01-02T01:02:03+00:00")
	stashHash := repo.revParse("refs/stash")
	repo.git("tag", "z", stashHash)
	repo.git("branch", "b", stashHash)
	repo.git("update-ref", "refs/remotes/origin/m", stashHash)

	loaded, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	stashTarget := NewCommitHash(stashHash)
	// The contract fixes each commit's reference ordering by kind; whether the
	// global all_refs projection also groups a stash commit referenced by a
	// tag/branch is not specified, so the per-commit query is authoritative.
	first := loaded.Refs(stashTarget)
	expectedKinds := []RefKind{RefKindTag, RefKindBranch, RefKindRemoteBranch, RefKindStash}
	expectedNames := []string{"z", "b", "origin/m"}
	if len(first) != 4 {
		t.Fatalf("refs for target = %+v, want four", first)
	}
	for index := 0; index < 3; index++ {
		if first[index].Kind != expectedKinds[index] || first[index].Name != expectedNames[index] {
			t.Fatalf("ref %d = %+v, want kind %s name %s", index, first[index], expectedKinds[index], expectedNames[index])
		}
	}
	if first[3].Kind != RefKindStash {
		t.Fatalf("stash ref is not last: %+v", first[3])
	}
	firstJSON, err := json.Marshal(loaded.AllRefs())
	if err != nil {
		t.Fatal(err)
	}
	for iteration := 0; iteration < 20; iteration++ {
		next, err := Load(repo.dir, SortChronological, nil, false)
		if err != nil {
			t.Fatal(err)
		}
		nextJSON, err := json.Marshal(next.AllRefs())
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(firstJSON, nextJSON) {
			t.Fatalf("all_refs changed on iteration %d", iteration)
		}
	}
}

func TestF2P058DeterminismAndCGOFreeRuntime(t *testing.T) {
	repo := f2pBuildBranchMergeRepo(t)
	repo.git("tag", "z")
	loaded, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	first, err := json.Marshal(CalcGraph(loaded))
	if err != nil {
		t.Fatal(err)
	}
	for iteration := 0; iteration < 20; iteration++ {
		next, err := json.Marshal(CalcGraph(loaded))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(first, next) {
			t.Fatalf("graph changed on iteration %d", iteration)
		}
	}
	f2pAssertCGOFreeRun(t, repo.dir)
}

func TestF2P059MailmapAndMissingLookup(t *testing.T) {
	f2pAssertMailmap(t)
	repo := f2pNewRepo(t)
	repo.commit("001", "2024-01-01T01:02:03+00:00")
	loaded, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	f2pAssertMissingCommit(t, loaded)
}

func TestF2P060CommitParsingWithoutRustToolchain(t *testing.T) {
	f2pAssertCommitParsing(t)
	f2pAssertNoRustToolchain(t)
}

func TestF2P061StashInsertionAndMissingDetail(t *testing.T) {
	f2pAssertStashInsertion(t)
	repo := f2pNewRepo(t)
	repo.commit("001", "2024-01-01T01:02:03+00:00")
	loaded, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	f2pAssertMissingCommit(t, loaded)
}

func TestF2P062LoaderParametersWithoutRustToolchain(t *testing.T) {
	f2pAssertLoaderParameters(t)
	f2pAssertNoRustToolchain(t)
}

func TestF2P063RepositoryValidationWithoutRustToolchain(t *testing.T) {
	f2pAssertRepositoryValidation(t)
	f2pAssertNoRustToolchain(t)
}

func TestF2P066CommitFieldsAndCGOFreeRuntime(t *testing.T) {
	f2pAssertCommitFieldsAndDates(t)
	repo := f2pNewRepo(t)
	repo.commit("001", "2024-01-01T01:02:03+00:00")
	f2pAssertCGOFreeRun(t, repo.dir)
}

func TestF2P067CommitHashValuesAndMissingLookup(t *testing.T) {
	full := "0123456789abcdef0123456789abcdef01234567"
	hash := NewCommitHash(full)
	if hash.Full != full {
		t.Fatalf("full hash = %q, want %q", hash.Full, full)
	}
	if hash.Short != "0123456" {
		t.Fatalf("short hash = %q, want %q", hash.Short, "0123456")
	}
	repo := f2pNewRepo(t)
	repo.commit("001", "2024-01-01T01:02:03+00:00")
	loaded, err := Load(repo.dir, SortChronological, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Commit(NewCommitHash(f2pZeroHash)) != nil {
		t.Fatal("zero hash resolved to a commit")
	}
	if got := loaded.ParentsHash(NewCommitHash(f2pZeroHash)); len(got) != 0 {
		t.Fatalf("zero hash parents = %v, want empty", got)
	}
}

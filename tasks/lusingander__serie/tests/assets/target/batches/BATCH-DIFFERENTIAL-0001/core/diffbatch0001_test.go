package core

import (
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

const (
	dif1AuthorName     = "Diff Author"
	dif1AuthorEmail    = "diff-author@example.com"
	dif1CommitterName  = "Diff Committer"
	dif1CommitterEmail = "diff-committer@example.com"
	dif1DefaultDate    = "2024-01-01T01:02:03+00:00"
)

type dif1Repo struct {
	t   *testing.T
	dir string
}

func dif1NewRepo(t *testing.T) *dif1Repo {
	t.Helper()
	r := &dif1Repo{t: t, dir: t.TempDir()}
	r.git("init", "-b", "master")
	return r
}

func (r *dif1Repo) git(args ...string) string {
	r.t.Helper()
	return r.gitWithDate(dif1DefaultDate, args...)
}

func (r *dif1Repo) gitWithDate(date string, args ...string) string {
	r.t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = r.dir
	cmd.Env = append(os.Environ(),
		"GIT_AUTHOR_NAME="+dif1AuthorName,
		"GIT_AUTHOR_EMAIL="+dif1AuthorEmail,
		"GIT_COMMITTER_NAME="+dif1CommitterName,
		"GIT_COMMITTER_EMAIL="+dif1CommitterEmail,
		"GIT_AUTHOR_DATE="+date,
		"GIT_COMMITTER_DATE="+date,
		"GIT_CONFIG_NOSYSTEM=true",
		"GIT_MERGE_AUTOEDIT=no",
		"HOME="+r.dir,
	)
	out, err := cmd.CombinedOutput()
	if err != nil {
		r.t.Fatalf("git %s failed: %v\n%s", strings.Join(args, " "), err, out)
	}
	return strings.TrimSpace(string(out))
}

func (r *dif1Repo) write(path, content string) {
	r.t.Helper()
	full := filepath.Join(r.dir, path)
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		r.t.Fatal(err)
	}
	if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
		r.t.Fatal(err)
	}
}

func (r *dif1Repo) commit(msg, date string) string {
	r.t.Helper()
	r.gitWithDate(date, "commit", "--allow-empty", "-m", msg)
	return r.revParse("HEAD")
}

func (r *dif1Repo) commitFile(msg, date, path, content string) string {
	r.t.Helper()
	r.write(path, content)
	r.git("add", "-A")
	r.gitWithDate(date, "commit", "-m", msg)
	return r.revParse("HEAD")
}

func (r *dif1Repo) revParse(ref string) string {
	r.t.Helper()
	return strings.TrimSpace(r.git("rev-parse", ref))
}

func dif1Load(t *testing.T, path string, order SortCommit, maxCount *int, mailmap bool) *Repository {
	t.Helper()
	repo, err := Load(path, order, maxCount, mailmap)
	if err != nil {
		t.Fatalf("Load(%q) failed: %v", path, err)
	}
	return repo
}

func dif1Int(n int) *int { return &n }

// DIFF-002, DIFF-003, DIFF-014: commit fields, fixed-offset RFC3339 dates, and
// full/seven-character short hashes.
func TestDiffB0001CommitRecordAndHash(t *testing.T) {
	r := dif1NewRepo(t)
	wantDate := "2024-02-03T04:05:06-05:00"
	full := r.commit("initial subject", wantDate)

	repo := dif1Load(t, r.dir, SortChronological, nil, false)
	commits := repo.AllCommits()
	if len(commits) != 1 {
		t.Fatalf("commit count = %d, want 1", len(commits))
	}
	commit := commits[0]
	if commit.CommitHash.Full != full {
		t.Fatalf("full hash = %q, want %q", commit.CommitHash.Full, full)
	}
	if commit.CommitHash.Short != full[:7] {
		t.Fatalf("short hash = %q, want %q", commit.CommitHash.Short, full[:7])
	}
	if commit.AuthorName != dif1AuthorName || commit.AuthorEmail != dif1AuthorEmail {
		t.Fatalf("author = %q <%q>", commit.AuthorName, commit.AuthorEmail)
	}
	if commit.CommitterName != dif1CommitterName || commit.CommitterEmail != dif1CommitterEmail {
		t.Fatalf("committer = %q <%q>", commit.CommitterName, commit.CommitterEmail)
	}
	if got := commit.AuthorDate.Format(time.RFC3339); got != wantDate {
		t.Fatalf("author date = %q, want %q", got, wantDate)
	}
	if got := commit.CommitterDate.Format(time.RFC3339); got != wantDate {
		t.Fatalf("committer date = %q, want %q", got, wantDate)
	}
	if commit.Subject != "initial subject" {
		t.Fatalf("subject = %q", commit.Subject)
	}
	if len(commit.ParentCommitHashes) != 0 {
		t.Fatalf("root parents = %v, want none", commit.ParentCommitHashes)
	}
	if repo.Commit(NewCommitHash(full)) == nil {
		t.Fatalf("Commit(%s) returned nil", full)
	}
}

// DIFF-007: root additions, diff statuses, and rename source/destination.
func TestDiffB0001CommitDetail(t *testing.T) {
	t.Run("file paths preserve Git filename bytes", func(t *testing.T) {
		r := dif1NewRepo(t)
		from := "\u76ee\u5f55/old\tname\n.txt"
		to := "\u76ee\u5f55/new\tname\n.txt"
		root := r.commitFile("root", dif1DefaultDate, from, "original content\n")
		loaded := dif1Load(t, r.dir, SortChronological, nil, false)
		_, changes := loaded.CommitDetail(NewCommitHash(root))
		if len(changes) != 1 || changes[0].Kind != FileChangeAdd || changes[0].Path != from {
			t.Fatalf("root file paths = %+v, want addition of %q", changes, from)
		}
		r.git("mv", from, to)
		r.git("commit", "-m", "rename")
		rename := r.revParse("HEAD")
		modified := r.commitFile("modify", dif1DefaultDate, to, "changed content\n")
		r.git("rm", to)
		r.git("commit", "-m", "delete")
		deleted := r.revParse("HEAD")
		loaded = dif1Load(t, r.dir, SortChronological, nil, false)
		for _, test := range []struct {
			hash string
			want FileChange
		}{
			{rename, FileChange{Kind: FileChangeRename, From: from, To: to}},
			{modified, FileChange{Kind: FileChangeModify, Path: to}},
			{deleted, FileChange{Kind: FileChangeDelete, Path: to}},
		} {
			_, changes := loaded.CommitDetail(NewCommitHash(test.hash))
			if len(changes) != 1 || changes[0] != test.want {
				t.Fatalf("file changes = %+v, want %+v", changes, test.want)
			}
		}
	})

	r := dif1NewRepo(t)
	root := r.commitFile("root", "2024-02-01T01:00:00+00:00", "a.txt", "one\n")
	mod := r.commitFile("modify", "2024-02-02T01:00:00+00:00", "a.txt", "two\n")
	r.git("mv", "a.txt", "b.txt")
	r.gitWithDate("2024-02-03T01:00:00+00:00", "commit", "-m", "rename")
	ren := r.revParse("HEAD")

	repo := dif1Load(t, r.dir, SortChronological, nil, false)

	_, rootChanges := repo.CommitDetail(NewCommitHash(root))
	if len(rootChanges) != 1 || rootChanges[0].Kind != FileChangeAdd || rootChanges[0].Path != "a.txt" {
		t.Fatalf("root changes = %+v, want add a.txt", rootChanges)
	}

	_, modChanges := repo.CommitDetail(NewCommitHash(mod))
	if len(modChanges) != 1 || modChanges[0].Kind != FileChangeModify || modChanges[0].Path != "a.txt" {
		t.Fatalf("modify changes = %+v, want modify a.txt", modChanges)
	}

	_, renChanges := repo.CommitDetail(NewCommitHash(ren))
	if len(renChanges) != 1 || renChanges[0].Kind != FileChangeRename ||
		renChanges[0].From != "a.txt" || renChanges[0].To != "b.txt" {
		t.Fatalf("rename changes = %+v, want rename a.txt -> b.txt", renChanges)
	}

	missing := NewCommitHash("0000000000000000000000000000000000000000")
	if repo.Commit(missing) != nil {
		t.Fatalf("Commit(zero hash) = non-nil")
	}
	if parents := repo.ParentsHash(missing); len(parents) != 0 {
		t.Fatalf("ParentsHash(zero hash) = %v, want empty", parents)
	}
	zeroCommit, zeroChanges := repo.CommitDetail(missing)
	if !zeroCommit.AuthorDate.IsZero() || len(zeroChanges) != 0 {
		t.Fatalf("CommitDetail(zero hash) = %+v, %v; want zero value and empty changes", zeroCommit, zeroChanges)
	}
}

// DIFF-004, DIFF-011, DIFF-013, DIFF-017, DIFF-018: ref kinds and ordering,
// stash message exposure, HEAD states, and validation errors.
func TestDiffB0001RefsStashHead(t *testing.T) {
	t.Run("failed Git output is never a partial repository", func(t *testing.T) {
		r := dif1NewRepo(t)
		r.commit("root", dif1DefaultDate)
		realGit, err := exec.LookPath("git")
		if err != nil {
			t.Fatal(err)
		}
		bin := t.TempDir()
		// Write real repository records, then signal producer failure. The
		// loader must honor the exit status even when the records parse.
		script := "#!/bin/sh\nfailed=0\nfor arg do\n  if [ \"$arg\" = log ]; then failed=1; fi\ndone\n" +
			"'" + strings.ReplaceAll(realGit, "'", "'\"'\"'") + "' \"$@\"\nstatus=$?\n" +
			"if [ \"$status\" -eq 0 ] && [ \"$failed\" -eq 1 ]; then exit 42; fi\nexit \"$status\"\n"
		if err := os.WriteFile(filepath.Join(bin, "git"), []byte(script), 0o755); err != nil {
			t.Fatal(err)
		}
		t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
		loaded, loadErr := Load(r.dir, SortChronological, nil, false)
		if loadErr == nil || loaded != nil {
			t.Fatalf("failed Git returned repository %v and error %v; want nil and an error", loaded, loadErr)
		}
	})

	t.Run("bare repository retains stash data", func(t *testing.T) {
		r := dif1NewRepo(t)
		root := r.commit("root", "2024-01-01T01:00:00+00:00")
		r.git("checkout", "-b", "stash-only")
		parent := r.commit("stash parent", "2024-01-02T01:00:00+00:00")
		var stashes []string
		for _, message := range []string{"first", "second"} {
			r.write("work.txt", message)
			r.gitWithDate("2024-01-03T01:00:00+00:00", "stash", "push", "--include-untracked", "-m", message)
			stashes = append(stashes, r.revParse("refs/stash"))
		}
		r.git("checkout", "master")
		r.git("branch", "-D", "stash-only")
		tip := r.commit("new tip", "2024-01-04T01:00:00+00:00")
		bare := filepath.Join(t.TempDir(), "bare.git")
		if err := os.CopyFS(bare, os.DirFS(filepath.Join(r.dir, ".git"))); err != nil {
			t.Fatal(err)
		}
		r.git("--git-dir="+bare, "config", "core.bare", "true")
		for _, order := range []SortCommit{SortChronological, SortTopological} {
			for _, limit := range []*int{nil, dif1Int(1)} {
				loaded := dif1Load(t, bare, order, limit, false)
				want := []string{tip, stashes[1], stashes[0], parent, root}
				if limit != nil {
					want = []string{tip}
				}
				var hashes []string
				for _, commit := range loaded.AllCommits() {
					hashes = append(hashes, commit.CommitHash.Full)
				}
				if !reflect.DeepEqual(hashes, want) {
					t.Fatalf("bare stash enumeration = %v, want %v", hashes, want)
				}
				for index, hash := range []string{stashes[1], stashes[0]} {
					refs := loaded.Refs(NewCommitHash(hash))
					name := "stash@{0}"
					message := "second"
					if index == 1 {
						name, message = "stash@{1}", "first"
					}
					if len(refs) != 1 || refs[0].Kind != RefKindStash || refs[0].Name != name ||
						refs[0].Target.Full != hash || !strings.Contains(refs[0].Message, message) {
						t.Fatalf("bare stash refs = %+v, want %s targeting %s", refs, name, hash)
					}
				}
			}
		}
	})
	r := dif1NewRepo(t)
	base := r.commit("base", "2024-01-01T01:00:00+00:00")
	r.git("tag", "zlite")
	r.gitWithDate("2024-01-02T01:00:00+00:00", "tag", "-a", "aannot", "-m", "annotated")
	r.git("branch", "bbranch")
	r.git("update-ref", "refs/remotes/origin/mmote", base)

	r.write("work.txt", "dirty\n")
	r.gitWithDate("2024-01-03T01:00:00+00:00", "stash", "push", "--include-untracked", "-m", "stash message")

	repo := dif1Load(t, r.dir, SortChronological, nil, false)

	kinds := map[RefKind]int{}
	var stash *Ref
	for i := range repo.AllRefs() {
		ref := repo.AllRefs()[i]
		kinds[ref.Kind]++
		if ref.Kind == RefKindStash {
			copied := ref
			stash = &copied
		}
	}
	for _, kind := range []RefKind{RefKindTag, RefKindBranch, RefKindRemoteBranch, RefKindStash} {
		if kinds[kind] == 0 {
			t.Fatalf("missing ref kind %q in %+v", kind, repo.AllRefs())
		}
	}
	if stash == nil || !strings.Contains(stash.Message, "stash message") {
		t.Fatalf("stash ref = %+v, want message containing %q", stash, "stash message")
	}

	firstIndex := func(kind RefKind) int {
		for i, ref := range repo.AllRefs() {
			if ref.Kind == kind {
				return i
			}
		}
		return -1
	}
	order := []RefKind{RefKindTag, RefKindBranch, RefKindRemoteBranch, RefKindStash}
	last := -1
	for _, kind := range order {
		idx := firstIndex(kind)
		if idx < last {
			t.Fatalf("ref kind order violated: %+v", repo.AllRefs())
		}
		last = idx
	}

	head := repo.Head()
	if head.Kind != HeadKindBranch || head.Name != "master" {
		t.Fatalf("HEAD = %+v, want branch master", head)
	}

	r.git("checkout", "--detach")
	detached := dif1Load(t, r.dir, SortChronological, nil, false)
	if detached.Head().Kind != HeadKindDetached || detached.Head().Target.Full != base {
		t.Fatalf("detached HEAD = %+v, want detached target %s", detached.Head(), base)
	}

	if _, err := Load(filepath.Join(r.dir, "does-not-exist"), SortChronological, nil, false); err == nil ||
		!strings.Contains(strings.ToLower(err.Error()), "not a git repository") {
		t.Fatalf("non-repository error = %v, want not a git repository", err)
	}
	empty := t.TempDir()
	if out, err := exec.Command("git", "init", "-b", "master", empty).CombinedOutput(); err != nil {
		t.Fatalf("git init empty: %v\n%s", err, out)
	}
	if _, err := Load(empty, SortChronological, nil, false); err == nil ||
		!strings.Contains(strings.ToLower(err.Error()), "no commits") {
		t.Fatalf("empty repository error = %v, want no commits in the repository", err)
	}
}

// DIFF-006, DIFF-015, DIFF-023: loader parameters, mailmap resolution, and
// deterministic repeated loads.
func TestDiffB0001LoaderOrderMailmapDeterminism(t *testing.T) {
	r := dif1NewRepo(t)
	r.commitFile("c1", "2024-01-01T01:00:00+00:00", "f.txt", "1\n")
	r.git("checkout", "-b", "side")
	r.commitFile("c2", "2024-01-02T01:00:00+00:00", "f.txt", "2\n")
	r.git("checkout", "master")
	r.commitFile("c3", "2024-01-03T01:00:00+00:00", "g.txt", "3\n")
	r.gitWithDate("2024-01-04T01:00:00+00:00", "merge", "--no-ff", "--no-log", "-m", "merge", "--no-edit", "side")

	dateOrder := strings.Fields(r.git("log", "--date-order", "--format=%H", "--all"))
	topoOrder := strings.Fields(r.git("log", "--topo-order", "--format=%H", "--all"))

	chrono := dif1Load(t, r.dir, SortChronological, nil, false)
	var gotChrono []string
	for _, commit := range chrono.AllCommits() {
		gotChrono = append(gotChrono, commit.CommitHash.Full)
	}
	if !reflect.DeepEqual(gotChrono, dateOrder) {
		t.Fatalf("chronological order = %v, want %v", gotChrono, dateOrder)
	}

	topo := dif1Load(t, r.dir, SortTopological, nil, false)
	var gotTopo []string
	for _, commit := range topo.AllCommits() {
		gotTopo = append(gotTopo, commit.CommitHash.Full)
	}
	if !reflect.DeepEqual(gotTopo, topoOrder) {
		t.Fatalf("topological order = %v, want %v", gotTopo, topoOrder)
	}

	limited := dif1Load(t, r.dir, SortChronological, dif1Int(2), false)
	if len(limited.AllCommits()) != 2 {
		t.Fatalf("max_count=2 loaded %d commits", len(limited.AllCommits()))
	}

	again := dif1Load(t, r.dir, SortChronological, nil, false)
	if !reflect.DeepEqual(chrono.AllCommits(), again.AllCommits()) {
		t.Fatalf("repeated loads differ")
	}

	// Mailmap is only relevant when .mailmap exists; when absent identities stay raw.
	if got := chrono.AllCommits()[0].AuthorName; got != dif1AuthorName {
		t.Fatalf("identity without .mailmap = %q, want %q", got, dif1AuthorName)
	}
	mapRepo := dif1NewRepo(t)
	mapRepo.write(".mailmap", "Mapped Name <mapped@example.com> Diff Author <diff-author@example.com>\n")
	mapRepo.commitFile("mapped", "2024-02-01T01:00:00+00:00", "x.txt", "x\n")
	raw := dif1Load(t, mapRepo.dir, SortChronological, nil, false)
	mapped := dif1Load(t, mapRepo.dir, SortChronological, nil, true)
	if raw.AllCommits()[0].AuthorName != dif1AuthorName {
		t.Fatalf("raw identity = %q", raw.AllCommits()[0].AuthorName)
	}
	if mapped.AllCommits()[0].AuthorName != "Mapped Name" {
		t.Fatalf("mapped identity = %q, want Mapped Name", mapped.AllCommits()[0].AuthorName)
	}
}

// DIFF-008, DIFF-009, DIFF-010, DIFF-019, DIFF-021, DIFF-022, DIFF-023:
// graph shape, lane range, row ordering/deduplication, and determinism.
func TestDiffB0001GraphInvariants(t *testing.T) {
	r := dif1NewRepo(t)
	r.commitFile("c1", "2024-01-01T01:00:00+00:00", "a.txt", "1\n")
	r.git("checkout", "-b", "one")
	r.commitFile("c2", "2024-01-02T01:00:00+00:00", "b.txt", "2\n")
	r.git("checkout", "master")
	r.commitFile("c3", "2024-01-03T01:00:00+00:00", "c.txt", "3\n")
	r.git("checkout", "-b", "two")
	r.commitFile("c4", "2024-01-04T01:00:00+00:00", "d.txt", "4\n")
	r.git("checkout", "master")
	r.gitWithDate("2024-01-05T01:00:00+00:00", "merge", "--no-ff", "--no-log", "-m", "merge one", "--no-edit", "one")
	r.gitWithDate("2024-01-06T01:00:00+00:00", "merge", "--no-ff", "--no-log", "-m", "merge two", "--no-edit", "two")

	repo := dif1Load(t, r.dir, SortTopological, nil, false)
	graph := CalcGraph(repo)
	if len(graph.Commits) != len(repo.AllCommits()) {
		t.Fatalf("graph commits = %d, want %d", len(graph.Commits), len(repo.AllCommits()))
	}
	if len(graph.Edges) != len(graph.Commits) {
		t.Fatalf("graph edge rows = %d, want %d", len(graph.Edges), len(graph.Commits))
	}
	for row, commit := range graph.Commits {
		pos, ok := graph.CommitPosMap[commit.CommitHash]
		if !ok {
			t.Fatalf("missing commit position for %s", commit.CommitHash.Full)
		}
		if pos.PosY != row {
			t.Fatalf("commit %s pos_y = %d, want row %d", commit.CommitHash.Full, pos.PosY, row)
		}
		seen := map[string]bool{}
		prevKey := ""
		for _, edge := range graph.Edges[row] {
			if edge.PosX < 0 || edge.PosX > graph.MaxPosX ||
				edge.AssociatedLinePosX < 0 || edge.AssociatedLinePosX > graph.MaxPosX {
				t.Fatalf("row %d edge out of lane range: %+v (max %d)", row, edge, graph.MaxPosX)
			}
			key := edge.EdgeKey()
			if seen[key] {
				t.Fatalf("row %d duplicate edge %+v", row, edge)
			}
			seen[key] = true
			if prevKey != "" && key < prevKey {
				t.Fatalf("row %d edges not sorted: %q before %q", row, prevKey, key)
			}
			prevKey = key
		}
	}
	if graph.MaxPosX < 1 {
		t.Fatalf("MaxPosX = %d, want branch lanes represented", graph.MaxPosX)
	}

	again := CalcGraph(dif1Load(t, r.dir, SortTopological, nil, false))
	if !reflect.DeepEqual(graph, again) {
		t.Fatalf("repeated graph calculation differs")
	}

	truncated := CalcGraph(dif1Load(t, r.dir, SortChronological, dif1Int(1), false))
	if len(truncated.Edges) != 1 {
		t.Fatalf("truncated rows = %d, want 1", len(truncated.Edges))
	}
	hasDown := false
	for _, edge := range truncated.Edges[0] {
		if edge.EdgeType == EdgeTypeDown {
			hasDown = true
		}
	}
	if !hasDown {
		t.Fatalf("truncated-parent row lacks down edge: %+v", truncated.Edges[0])
	}
}

// DIFF-012: core config semantics survive the existing TOML layout.
func TestDiffB0001CoreConfigSemantics(t *testing.T) {
	if candidateCoreConfigParser {
		// These are equivalent TOML representations of the existing core keys.
		// Read the candidate's optional parser through the binding, not a
		// replacement implementation owned by the evaluator.
		for _, content := range []string{
			`core = { option = { order = "topo" }, git = { mailmap = true } }`,
			"[core]\noption = { order = 'topo' }\ngit = { mailmap = true }\n",
			"core.option.order = 'topo'\ncore.git.mailmap = true\n",
		} {
			config, err := ParseCoreConfig(content)
			if err != nil || config.Order != SortTopological || !config.Mailmap {
				t.Fatalf("candidate config for %q = %+v, %v; want topo/true", content, config, err)
			}
		}
	}
	content := `
# UI-only sections must be ignored by the core.
[ui]
theme = "dark"

[core.option]
order = "topo"

[core.git]
mailmap = true
`
	cfg, err := ParseCoreConfig(content)
	if err != nil {
		t.Fatalf("ParseCoreConfig failed: %v", err)
	}
	if cfg.Order != SortTopological {
		t.Fatalf("order = %v, want topo", cfg.Order)
	}
	if !cfg.Mailmap {
		t.Fatalf("mailmap = false, want true")
	}

	chrono, err := ParseCoreConfig("[core.option]\norder = \"chrono\"\n[core.git]\nmailmap = false\n")
	if err != nil {
		t.Fatalf("ParseCoreConfig chrono failed: %v", err)
	}
	if chrono.Order != SortChronological || chrono.Mailmap {
		t.Fatalf("chrono config = %+v, want chrono/false", chrono)
	}
}

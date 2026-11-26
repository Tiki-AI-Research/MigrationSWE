package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"time"

	"github.com/lusingander/serie/core"
)

const zeroHash = "0000000000000000000000000000000000000000"

const (
	authorName     = "Author Name"
	authorEmail    = "author@example.com"
	committerName  = "Committer Name"
	committerEmail = "committer@example.com"
	defaultDate    = "2024-01-01T01:02:03+00:00"
)

type result struct {
	Observation string `json:"observation"`
	Status      string `json:"status"`
	Detail      string `json:"detail"`
}

type fixture struct {
	dir string
}

type complexFixture struct {
	f       *fixture
	root    string
	feature string
	master  string
	merge   string
}

func main() {
	if len(os.Args) < 3 {
		fmt.Fprintln(os.Stderr, "usage: probe <observation-id> <repository-path>")
		os.Exit(2)
	}
	observation := os.Args[1]
	repoPath := os.Args[2]
	status := "PASS"
	detail := "target postcondition observed"
	if err := runCaseSafely(observation, repoPath); err != nil {
		status = "FAIL"
		detail = err.Error()
	}
	payload, _ := json.Marshal(result{Observation: observation, Status: status, Detail: detail})
	fmt.Println(string(payload))
	if status != "PASS" {
		os.Exit(1)
	}
}

// runCaseSafely turns a panic in the candidate's migrated core into an ordinary
// observation failure. A candidate that crashes on a contract query has failed
// that observation; it must not be reported as an evaluator infrastructure
// error, which would make the whole verifier result indeterminate.
func runCaseSafely(observation, repoPath string) (err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			err = fmt.Errorf("candidate core panicked: %v", recovered)
		}
	}()
	return runCase(observation, repoPath)
}

func runCase(observation, repoPath string) error {
	switch observation {
	case "F2P-001":
		return caseMissingCommit(repoPath, "references")
	case "F2P-002":
		return caseGitWrapper(repoPath, "references")
	case "F2P-003":
		return caseGitWrapper(repoPath, "reference-loading")
	case "F2P-004":
		return caseMissingCommit(repoPath, "detail-changes")
	case "F2P-005":
		return caseAllRefsOrder(repoPath)
	case "F2P-006":
		return caseNoRust(repoPath, "stash")
	case "F2P-007":
		return caseNoRust(repoPath, "determinism")
	case "F2P-008":
		return caseLoadGraph(repoPath, "stash")
	case "F2P-009":
		return caseMissingDetail(repoPath, "mailmap")
	case "F2P-010":
		return caseLoadGraph(repoPath, "queries")
	case "F2P-011":
		return caseCgoNoRust(repoPath)
	case "F2P-012":
		return caseNoRust(repoPath, "reference-loading")
	case "F2P-013":
		return caseGitWrapper(repoPath, "commit-fields")
	case "F2P-014":
		return caseGraphInvariants(repoPath)
	case "F2P-015":
		return caseCLIJSON(repoPath, "runtime-entry")
	case "F2P-016":
		return caseMissingDetail(repoPath, "detail-changes")
	case "F2P-017":
		return caseNoRust(repoPath, "reference-order")
	case "F2P-018":
		return caseNoRust(repoPath, "detail-changes")
	case "F2P-019":
		return caseNoRust(repoPath, "enumeration")
	case "F2P-020":
		return caseCLIJSON(repoPath, "cli-options")
	case "F2P-021":
		return caseLoadGraph(repoPath, "references")
	case "F2P-022":
		return caseMissingCommit(repoPath, "enumeration")
	case "F2P-023":
		return caseNoRust(repoPath, "mailmap")
	case "F2P-024":
		return caseBuildBehavior(repoPath)
	default:
		return fmt.Errorf("unknown observation %s", observation)
	}
}

func newFixture() (*fixture, error) {
	dir, err := os.MkdirTemp("", "serie-f2p-")
	if err != nil {
		return nil, err
	}
	f := &fixture{dir: dir}
	if _, err := f.git(defaultDate, "init", "-b", "master"); err != nil {
		return nil, err
	}
	return f, nil
}

func (f *fixture) git(date string, args ...string) (string, error) {
	cmd := exec.Command("git", args...)
	cmd.Dir = f.dir
	cmd.Env = append(os.Environ(),
		"GIT_AUTHOR_NAME="+authorName,
		"GIT_AUTHOR_EMAIL="+authorEmail,
		"GIT_COMMITTER_NAME="+committerName,
		"GIT_COMMITTER_EMAIL="+committerEmail,
		"GIT_AUTHOR_DATE="+date,
		"GIT_COMMITTER_DATE="+date,
		"GIT_CONFIG_NOSYSTEM=1",
		"GIT_MERGE_AUTOEDIT=no",
		"HOME="+f.dir,
	)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return strings.TrimSpace(stdout.String()), fmt.Errorf("git %s: %v: %s", strings.Join(args, " "), err, strings.TrimSpace(stderr.String()))
	}
	return strings.TrimSpace(stdout.String()), nil
}

func (f *fixture) writeFile(name, content string) error {
	path := filepath.Join(f.dir, name)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, []byte(content), 0o644)
}

func (f *fixture) commitFile(message, date, name, content string) (string, error) {
	if err := f.writeFile(name, content); err != nil {
		return "", err
	}
	if _, err := f.git(date, "add", "-A"); err != nil {
		return "", err
	}
	if _, err := f.git(date, "commit", "-m", message); err != nil {
		return "", err
	}
	return f.revParse("HEAD")
}

func (f *fixture) commitEmpty(message, date string) (string, error) {
	if _, err := f.git(date, "commit", "--allow-empty", "-m", message); err != nil {
		return "", err
	}
	return f.revParse("HEAD")
}

func (f *fixture) checkout(name string) error {
	_, err := f.git(defaultDate, "checkout", name)
	return err
}

func (f *fixture) checkoutNew(name string) error {
	_, err := f.git(defaultDate, "checkout", "-b", name)
	return err
}

func (f *fixture) merge(date, branch string) (string, error) {
	if _, err := f.git(date, "merge", "--no-ff", "--no-log", "-m", "merge "+branch, "--no-edit", branch); err != nil {
		return "", err
	}
	return f.revParse("HEAD")
}

func (f *fixture) tagAnnotated(name, target, date string) error {
	_, err := f.git(date, "tag", "-a", name, "-m", "tag "+name, target)
	return err
}

func (f *fixture) tagLight(name, target string) error {
	_, err := f.git(defaultDate, "tag", name, target)
	return err
}

func (f *fixture) branch(name, target string) error {
	_, err := f.git(defaultDate, "branch", name, target)
	return err
}

func (f *fixture) updateRef(name, target string) error {
	_, err := f.git(defaultDate, "update-ref", name, target)
	return err
}

func (f *fixture) stash(date, message string) (string, error) {
	name := fmt.Sprintf("stash-%d.txt", time.Now().UnixNano())
	if err := f.writeFile(name, message); err != nil {
		return "", err
	}
	if _, err := f.git(date, "stash", "push", "--include-untracked", "-m", message); err != nil {
		return "", err
	}
	return f.revParse("refs/stash")
}

func (f *fixture) revParse(ref string) (string, error) {
	return f.git(defaultDate, "rev-parse", ref)
}

func (f *fixture) logHashes(order string) ([]string, error) {
	out, err := f.git(defaultDate, "log", order, "--pretty=%H", "--branches", "--remotes", "--tags", "HEAD")
	if err != nil {
		return nil, err
	}
	if out == "" {
		return nil, nil
	}
	return strings.Fields(out), nil
}

func makeComplexFixture() (*complexFixture, error) {
	f, err := newFixture()
	if err != nil {
		return nil, err
	}
	info := &complexFixture{f: f}
	if info.root, err = f.commitFile("root", "2024-01-01T01:02:03+00:00", "a.txt", "one"); err != nil {
		return nil, err
	}
	if err := f.checkoutNew("feature"); err != nil {
		return nil, err
	}
	if info.feature, err = f.commitFile("feature", "2024-01-02T01:02:03+00:00", "b.txt", "two"); err != nil {
		return nil, err
	}
	if err := f.checkout("master"); err != nil {
		return nil, err
	}
	if info.master, err = f.commitFile("master", "2024-01-03T01:02:03-05:00", "a.txt", "three"); err != nil {
		return nil, err
	}
	if info.merge, err = f.merge("2024-01-04T01:02:03+00:00", "feature"); err != nil {
		return nil, err
	}
	if err := f.tagAnnotated("z", info.merge, "2024-01-05T01:02:03+00:00"); err != nil {
		return nil, err
	}
	if err := f.tagLight("light", info.root); err != nil {
		return nil, err
	}
	if err := f.branch("b", info.merge); err != nil {
		return nil, err
	}
	if err := f.updateRef("refs/remotes/origin/m", info.merge); err != nil {
		return nil, err
	}
	if _, err := f.stash("2024-01-06T01:02:03+00:00", "stash one"); err != nil {
		return nil, err
	}
	return info, nil
}

func makeBranchMergeFixture() (*fixture, string, error) {
	f, err := newFixture()
	if err != nil {
		return nil, "", err
	}
	root, err := f.commitFile("root", "2024-01-01T01:02:03+00:00", "a.txt", "one")
	if err != nil {
		return nil, "", err
	}
	if err := f.checkoutNew("feature"); err != nil {
		return nil, "", err
	}
	if _, err := f.commitFile("feature", "2024-01-02T01:02:03+00:00", "b.txt", "two"); err != nil {
		return nil, "", err
	}
	if err := f.checkout("master"); err != nil {
		return nil, "", err
	}
	if _, err := f.commitFile("master", "2024-01-03T01:02:03+00:00", "a.txt", "three"); err != nil {
		return nil, "", err
	}
	if _, err := f.merge("2024-01-04T01:02:03+00:00", "feature"); err != nil {
		return nil, "", err
	}
	if err := f.branch("b", "HEAD"); err != nil {
		return nil, "", err
	}
	if err := f.updateRef("refs/remotes/origin/m", "HEAD"); err != nil {
		return nil, "", err
	}
	return f, root, nil
}

func makeDetailFixture() (*fixture, string, string, error) {
	f, err := newFixture()
	if err != nil {
		return nil, "", "", err
	}
	if err := f.writeFile("keep.txt", "keep"); err != nil {
		return nil, "", "", err
	}
	if err := f.writeFile("modify.txt", "old"); err != nil {
		return nil, "", "", err
	}
	if err := f.writeFile("delete.txt", "delete"); err != nil {
		return nil, "", "", err
	}
	if _, err := f.git("2024-01-01T01:02:03+00:00", "add", "-A"); err != nil {
		return nil, "", "", err
	}
	if _, err := f.git("2024-01-01T01:02:03+00:00", "commit", "-m", "base"); err != nil {
		return nil, "", "", err
	}
	root, err := f.revParse("HEAD")
	if err != nil {
		return nil, "", "", err
	}
	if err := f.writeFile("modify.txt", "new"); err != nil {
		return nil, "", "", err
	}
	if err := os.Remove(filepath.Join(f.dir, "delete.txt")); err != nil {
		return nil, "", "", err
	}
	if err := f.writeFile("added.txt", "added"); err != nil {
		return nil, "", "", err
	}
	if _, err := f.git(defaultDate, "mv", "keep.txt", "renamed.txt"); err != nil {
		return nil, "", "", err
	}
	if _, err := f.git("2024-01-02T01:02:03+00:00", "add", "-A"); err != nil {
		return nil, "", "", err
	}
	if _, err := f.git("2024-01-02T01:02:03+00:00", "commit", "-m", "changes"); err != nil {
		return nil, "", "", err
	}
	changes, err := f.revParse("HEAD")
	if err != nil {
		return nil, "", "", err
	}
	return f, root, changes, nil
}

func makeMailmapFixture() (*fixture, string, error) {
	f, err := newFixture()
	if err != nil {
		return nil, "", err
	}
	hash, err := f.commitEmpty("001", "2024-01-01T01:02:03+00:00")
	if err != nil {
		return nil, "", err
	}
	mailmap := "New Author <new-author@example.com> " + authorName + " <" + authorEmail + ">\n" +
		"New Committer <new-committer@example.com> " + committerName + " <" + committerEmail + ">\n"
	if err := f.writeFile(".mailmap", mailmap); err != nil {
		return nil, "", err
	}
	return f, hash, nil
}

func loadComplex(repoPath string) (*core.Repository, *complexFixture, error) {
	if _, err := os.Stat(filepath.Join(repoPath, "go.mod")); err != nil {
		return nil, nil, fmt.Errorf("target Go module is absent: %w", err)
	}
	info, err := makeComplexFixture()
	if err != nil {
		return nil, nil, err
	}
	repo, err := core.Load(info.f.dir, core.SortChronological, nil, false)
	if err != nil {
		return nil, nil, err
	}
	return repo, info, nil
}

func caseMissingCommit(repoPath, surface string) error {
	repo, info, err := loadComplex(repoPath)
	if err != nil {
		return err
	}
	missing := core.NewCommitHash(zeroHash)
	if got := repo.Commit(missing); got != nil {
		return fmt.Errorf("commit lookup for missing hash returned %+v", got)
	}
	if got := repo.ParentsHash(missing); len(got) != 0 {
		return fmt.Errorf("parents lookup for missing hash returned %v", got)
	}
	switch surface {
	case "references":
		return checkReferenceLoading(repo, info)
	case "detail-changes":
		return checkCommitDetail(repo, info)
	case "enumeration":
		return checkEnumeration()
	default:
		return fmt.Errorf("unknown surface %s", surface)
	}
}

func caseMissingDetail(repoPath, surface string) error {
	repo, info, err := loadComplex(repoPath)
	if err != nil {
		return err
	}
	commit, changes := repo.CommitDetail(core.NewCommitHash(zeroHash))
	if !zeroCommit(commit) {
		return fmt.Errorf("commit detail for missing hash returned %+v", commit)
	}
	if len(changes) != 0 {
		return fmt.Errorf("commit detail for missing hash returned changes %+v", changes)
	}
	switch surface {
	case "mailmap":
		return checkMailmap()
	case "detail-changes":
		return checkCommitDetail(repo, info)
	default:
		return fmt.Errorf("unknown surface %s", surface)
	}
}

// zeroCommit reports whether a commit is the zero value. It ignores the
// nil-versus-empty distinction for the parent slice, which the contract leaves
// unspecified, so a legal port that returns an empty slice is not misjudged.
func zeroCommit(commit core.Commit) bool {
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

func caseGitWrapper(repoPath, surface string) error {
	if _, err := os.Stat(filepath.Join(repoPath, "go.mod")); err != nil {
		return fmt.Errorf("target Go module is absent: %w", err)
	}
	info, err := makeComplexFixture()
	if err != nil {
		return err
	}
	bin, err := buildCLI(repoPath)
	if err != nil {
		return err
	}
	wrapperDir, logPath, env, err := makeGitWrapper()
	if err != nil {
		return err
	}
	defer os.RemoveAll(wrapperDir)
	stdout, stderr, runErr := runCommand(bin, []string{info.f.dir}, env)
	if runErr != nil {
		return fmt.Errorf("CLI failed with git wrapper: %v: %s", runErr, stderr)
	}
	logData, err := os.ReadFile(logPath)
	if err != nil {
		return err
	}
	logText := string(logData)
	for _, required := range []string{"show-ref --head --dereference", "log", "stash list"} {
		if !strings.Contains(logText, required) {
			return fmt.Errorf("git wrapper log does not contain %q: %s", required, logText)
		}
	}
	object, err := parseJSONObject(stdout)
	if err != nil {
		return err
	}
	if !hasNonEmptyArray(object, "commits") {
		return fmt.Errorf("CLI output did not include loaded commits")
	}
	repo, err := core.Load(info.f.dir, core.SortChronological, nil, false)
	if err != nil {
		return err
	}
	switch surface {
	case "references":
		return checkReferenceKinds(repo)
	case "reference-loading":
		return checkReferenceLoading(repo, info)
	case "commit-fields":
		return checkCommitFields(repo, info)
	default:
		return fmt.Errorf("unknown surface %s", surface)
	}
}

func caseAllRefsOrder(repoPath string) error {
	repo, info, err := loadComplex(repoPath)
	if err != nil {
		return err
	}
	first := repo.AllRefs()
	for i := 0; i < 20; i++ {
		next := repo.AllRefs()
		if !reflect.DeepEqual(first, next) {
			return fmt.Errorf("all_refs order changed on iteration %d", i)
		}
	}
	// The contract orders each commit's references by kind; it does not fix a
	// global ordering across different commits, so verify that per-commit rule.
	for _, commit := range repo.AllCommits() {
		refs := repo.Refs(commit.CommitHash)
		for i := 1; i < len(refs); i++ {
			if core.CompareRefs(refs[i-1], refs[i]) > 0 {
				return fmt.Errorf("refs for %s are not ordered: %+v", commit.CommitHash.Full, refs)
			}
		}
	}
	var onMerge []string
	mergeHash := core.NewCommitHash(info.merge)
	seenKinds := map[core.RefKind]bool{}
	for _, ref := range first {
		seenKinds[ref.Kind] = true
	}
	for _, ref := range repo.Refs(mergeHash) {
		if ref.Target == mergeHash {
			onMerge = append(onMerge, string(ref.Kind))
		}
	}
	expected := []string{"tag", "branch", "branch", "remote_branch"}
	if !reflect.DeepEqual(onMerge, expected) {
		return fmt.Errorf("refs order for merge commit = %v, want %v", onMerge, expected)
	}
	for _, kind := range []core.RefKind{core.RefKindTag, core.RefKindBranch, core.RefKindRemoteBranch, core.RefKindStash} {
		if !seenKinds[kind] {
			return fmt.Errorf("all_refs is missing reference kind %s", kind)
		}
	}
	return nil
}

func caseNoRust(repoPath, surface string) error {
	if _, err := os.Stat(filepath.Join(repoPath, "go.mod")); err != nil {
		return fmt.Errorf("target Go module is absent: %w", err)
	}
	if err := checkNoRustReferences(repoPath); err != nil {
		return err
	}
	info, err := makeComplexFixture()
	if err != nil {
		return err
	}
	repo, err := core.Load(info.f.dir, core.SortChronological, nil, false)
	if err != nil {
		return err
	}
	switch surface {
	case "stash":
		if err := checkStashInsertion(repo); err != nil {
			return err
		}
	case "determinism":
		if err := checkDeterminism(repo); err != nil {
			return err
		}
	case "reference-loading":
		if err := checkReferenceLoading(repo, info); err != nil {
			return err
		}
	case "reference-order":
		if err := checkReferenceOrder(repo); err != nil {
			return err
		}
	case "detail-changes":
		if err := checkCommitDetail(repo, info); err != nil {
			return err
		}
	case "enumeration":
		if err := checkEnumeration(); err != nil {
			return err
		}
	case "mailmap":
		if err := checkMailmap(); err != nil {
			return err
		}
	default:
		return fmt.Errorf("unknown surface %s", surface)
	}

	bin, err := buildCLI(repoPath)
	if err != nil {
		return err
	}
	sanitized, err := sanitizedPATH()
	if err != nil {
		return err
	}
	defer os.RemoveAll(sanitized)
	env := []string{"PATH=" + sanitized, "HOME=" + sanitized, "CGO_ENABLED=0", "GOTOOLCHAIN=local", "GOPROXY=off"}
	stdout, stderr, runErr := runCommand(bin, []string{info.f.dir}, env)
	if runErr != nil {
		return fmt.Errorf("CLI failed without Rust toolchain: %v: %s", runErr, stderr)
	}
	object, err := parseJSONObject(stdout)
	if err != nil {
		return err
	}
	if !hasNonEmptyArray(object, "commits") || !hasNonEmptyArray(object, "all_refs") {
		return fmt.Errorf("CLI did not load commits and refs without Rust toolchain")
	}
	return runGoTest(repoPath, env)
}

func caseCgoNoRust(repoPath string) error {
	if _, err := os.Stat(filepath.Join(repoPath, "go.mod")); err != nil {
		return fmt.Errorf("target Go module is absent: %w", err)
	}
	if err := checkNoCGOResidue(repoPath); err != nil {
		return err
	}
	info, err := makeComplexFixture()
	if err != nil {
		return err
	}
	env, err := sanitizedEnv()
	if err != nil {
		return err
	}
	defer os.RemoveAll(env.path)
	buildEnv := append(env.vars, "CGO_ENABLED=0")
	if err := runGoBuild(repoPath, buildEnv); err != nil {
		return err
	}
	bin, err := buildCLIWithEnv(repoPath, buildEnv)
	if err != nil {
		return err
	}
	stdout, stderr, runErr := runCommand(bin, []string{info.f.dir}, env.vars)
	if runErr != nil {
		return fmt.Errorf("CLI failed with CGO disabled and no Rust toolchain: %v: %s", runErr, stderr)
	}
	object, err := parseJSONObject(stdout)
	if err != nil {
		return err
	}
	if !hasNonEmptyArray(object, "commits") {
		return fmt.Errorf("CLI did not load commits")
	}
	return nil
}

func caseLoadGraph(repoPath, surface string) error {
	repo, info, err := loadComplex(repoPath)
	if err != nil {
		return err
	}
	graph := core.CalcGraph(repo)
	if len(graph.Commits) != len(repo.AllCommits()) || len(graph.Edges) != len(graph.Commits) {
		return fmt.Errorf("graph shape mismatch: commits=%d edges=%d", len(graph.Commits), len(graph.Edges))
	}
	if len(repo.AllCommits()) == 0 || len(repo.AllRefs()) == 0 {
		return fmt.Errorf("repository did not load commits and references")
	}
	if graph.MaxPosX < 0 {
		return fmt.Errorf("negative max lane %d", graph.MaxPosX)
	}
	switch surface {
	case "stash":
		return checkStashInsertion(repo)
	case "queries":
		return checkQueryAPI(repo, info)
	case "references":
		return checkReferenceLoading(repo, info)
	default:
		return fmt.Errorf("unknown surface %s", surface)
	}
}

func caseGraphInvariants(repoPath string) error {
	if _, err := os.Stat(filepath.Join(repoPath, "go.mod")); err != nil {
		return fmt.Errorf("target Go module is absent: %w", err)
	}
	f, _, err := makeBranchMergeFixture()
	if err != nil {
		return err
	}
	repo, err := core.Load(f.dir, core.SortChronological, nil, false)
	if err != nil {
		return err
	}
	graph := core.CalcGraph(repo)
	if graph.MaxPosX < 0 {
		return fmt.Errorf("negative max lane %d", graph.MaxPosX)
	}
	for row, edges := range graph.Edges {
		for _, edge := range edges {
			if edge.PosX < 0 || edge.PosX > graph.MaxPosX {
				return fmt.Errorf("row %d edge pos_x %d outside [0,%d]", row, edge.PosX, graph.MaxPosX)
			}
			if edge.AssociatedLinePosX < 0 || edge.AssociatedLinePosX > graph.MaxPosX {
				return fmt.Errorf("row %d associated_line_pos_x %d outside [0,%d]", row, edge.AssociatedLinePosX, graph.MaxPosX)
			}
		}
	}
	for row, commit := range graph.Commits {
		pos, ok := graph.Position(commit.CommitHash)
		if !ok {
			return fmt.Errorf("commit %s missing from position map", commit.CommitHash.Full)
		}
		if pos.PosY != row {
			return fmt.Errorf("commit row %d has pos_y %d", row, pos.PosY)
		}
		if pos.PosX > graph.MaxPosX {
			return fmt.Errorf("commit row %d pos_x %d exceeds max %d", row, pos.PosX, graph.MaxPosX)
		}
	}
	return nil
}

func caseCLIJSON(repoPath, surface string) error {
	if _, err := os.Stat(filepath.Join(repoPath, "go.mod")); err != nil {
		return fmt.Errorf("target Go module is absent: %w", err)
	}
	f, err := newFixture()
	if err != nil {
		return err
	}
	if _, err := f.commitFile("one", "2024-02-03T04:05:06-05:00", "a.txt", "one"); err != nil {
		return err
	}
	bin, err := buildCLI(repoPath)
	if err != nil {
		return err
	}
	args := []string{f.dir}
	if surface == "cli-options" {
		args = append([]string{"-n", "1", "-o", "chrono"}, args...)
	}
	stdout, stderr, runErr := runCommand(bin, args, nil)
	if runErr != nil {
		return fmt.Errorf("CLI failed: %v: %s", runErr, stderr)
	}
	object, err := parseJSONObject(stdout)
	if err != nil {
		return err
	}
	for _, key := range []string{"commits", "commit_pos_map", "edges", "max_pos_x", "all_refs", "head"} {
		if _, ok := object[key]; !ok {
			return fmt.Errorf("CLI JSON missing %q", key)
		}
	}
	commits, ok := object["commits"].([]any)
	if !ok || len(commits) == 0 {
		return fmt.Errorf("CLI JSON has no commits")
	}
	commit, ok := commits[0].(map[string]any)
	if !ok {
		return fmt.Errorf("CLI commit has unexpected shape")
	}
	if _, ok := commit["commit_hash"].(string); !ok {
		return fmt.Errorf("commit_hash is not a string")
	}
	date, ok := commit["author_date"].(string)
	if !ok {
		return fmt.Errorf("author_date is not a string")
	}
	if _, err := time.Parse(time.RFC3339, date); err != nil {
		return fmt.Errorf("author_date is not RFC3339: %v", err)
	}
	if surface == "runtime-entry" {
		if !strings.Contains(date, "-05:00") {
			return fmt.Errorf("author_date did not retain fixed offset: %s", date)
		}
	}
	return nil
}

func caseBuildBehavior(repoPath string) error {
	if _, err := os.Stat(filepath.Join(repoPath, "go.mod")); err != nil {
		return fmt.Errorf("target Go module is absent: %w", err)
	}
	if err := runGoBuild(repoPath, nil); err != nil {
		return err
	}
	info, err := makeComplexFixture()
	if err != nil {
		return err
	}
	bin, err := buildCLI(repoPath)
	if err != nil {
		return err
	}
	stdout, stderr, runErr := runCommand(bin, []string{info.f.dir}, nil)
	if runErr != nil {
		return fmt.Errorf("CLI failed: %v: %s", runErr, stderr)
	}
	object, err := parseJSONObject(stdout)
	if err != nil {
		return err
	}
	if !hasNonEmptyArray(object, "commits") || !hasNonEmptyArray(object, "all_refs") || !hasNonEmptyArray(object, "edges") {
		return fmt.Errorf("CLI output did not include commits, refs, and graph edges")
	}
	return nil
}

func checkReferenceKinds(repo *core.Repository) error {
	kinds := map[core.RefKind]bool{}
	head := repo.Head()
	if head.Kind != core.HeadKindBranch || head.Name == "" {
		return fmt.Errorf("unexpected HEAD state: %+v", head)
	}
	for _, ref := range repo.AllRefs() {
		kinds[ref.Kind] = true
		if ref.Kind == core.RefKindStash && ref.Message == "" {
			return fmt.Errorf("stash reference has no message: %+v", ref)
		}
	}
	for _, kind := range []core.RefKind{core.RefKindTag, core.RefKindBranch, core.RefKindRemoteBranch, core.RefKindStash} {
		if !kinds[kind] {
			return fmt.Errorf("reference kind %s was not loaded", kind)
		}
	}
	return nil
}

func checkReferenceLoading(repo *core.Repository, info *complexFixture) error {
	if err := checkReferenceKinds(repo); err != nil {
		return err
	}
	tags, branches, remotes := map[string]string{}, map[string]string{}, map[string]string{}
	for _, ref := range repo.AllRefs() {
		switch ref.Kind {
		case core.RefKindTag:
			tags[ref.Name] = ref.Target.Full
		case core.RefKindBranch:
			branches[ref.Name] = ref.Target.Full
		case core.RefKindRemoteBranch:
			remotes[ref.Name] = ref.Target.Full
		}
	}
	if tags["z"] != info.merge {
		return fmt.Errorf("annotated tag z target = %q, want dereferenced commit %q", tags["z"], info.merge)
	}
	if tags["light"] != info.root {
		return fmt.Errorf("lightweight tag light target = %q, want %q", tags["light"], info.root)
	}
	if branches["b"] != info.merge {
		return fmt.Errorf("branch b target = %q, want %q", branches["b"], info.merge)
	}
	if remotes["origin/m"] != info.merge {
		return fmt.Errorf("remote branch origin/m target = %q, want %q", remotes["origin/m"], info.merge)
	}
	one := 1
	limited, err := core.Load(info.f.dir, core.SortChronological, &one, false)
	if err != nil {
		return err
	}
	limitedNames := map[string]bool{}
	for _, ref := range limited.AllRefs() {
		limitedNames[string(ref.Kind)+"/"+ref.Name] = true
	}
	for _, required := range []string{"tag/z", "tag/light", "branch/b", "remote_branch/origin/m"} {
		if !limitedNames[required] {
			return fmt.Errorf("max_count changed reference loading, missing %s", required)
		}
	}
	return nil
}

func checkStashInsertion(repo *core.Repository) error {
	commits := repo.AllCommits()
	index := map[string]int{}
	for i, commit := range commits {
		index[commit.CommitHash.Full] = i
	}
	found := false
	for _, ref := range repo.AllRefs() {
		if ref.Kind != core.RefKindStash {
			continue
		}
		found = true
		parents := repo.ParentsHash(ref.Target)
		if len(parents) == 0 {
			return fmt.Errorf("stash %s has no parent", ref.Name)
		}
		stashRow, ok := index[ref.Target.Full]
		if !ok {
			return fmt.Errorf("stash %s missing from commit sequence", ref.Name)
		}
		parentRow, ok := index[parents[0].Full]
		if !ok {
			return fmt.Errorf("stash parent %s missing from commit sequence", parents[0].Full)
		}
		if stashRow != parentRow-1 {
			return fmt.Errorf("stash row %d is not immediately before parent row %d", stashRow, parentRow)
		}
	}
	if !found {
		return fmt.Errorf("no stash reference was loaded")
	}
	return nil
}

func checkDeterminism(repo *core.Repository) error {
	firstGraph := core.CalcGraph(repo)
	firstState, err := snapshot(repo, firstGraph)
	if err != nil {
		return err
	}
	for i := 0; i < 5; i++ {
		graph := core.CalcGraph(repo)
		next, err := snapshot(repo, graph)
		if err != nil {
			return err
		}
		if next != firstState {
			return fmt.Errorf("repeated graph calculation changed result on iteration %d", i)
		}
	}
	return nil
}

func snapshot(repo *core.Repository, graph *core.Graph) (string, error) {
	hashes := make([]string, 0, len(graph.Commits))
	for _, commit := range graph.Commits {
		hashes = append(hashes, commit.CommitHash.Full)
	}
	refs := make([]string, 0)
	for _, ref := range repo.AllRefs() {
		refs = append(refs, fmt.Sprintf("%s/%s/%s/%s", ref.Kind, ref.Name, ref.Target.Full, ref.Message))
	}
	data, err := json.Marshal(struct {
		Hashes []string
		Refs   []string
		Edges  [][]core.Edge
		Max    int
	}{hashes, refs, graph.Edges, graph.MaxPosX})
	if err != nil {
		return "", err
	}
	return string(data), nil
}

func checkReferenceOrder(repo *core.Repository) error {
	for _, commit := range repo.AllCommits() {
		refs := repo.Refs(commit.CommitHash)
		for i := 1; i < len(refs); i++ {
			if core.CompareRefs(refs[i-1], refs[i]) > 0 {
				return fmt.Errorf("refs for %s are not ordered: %+v", commit.CommitHash.Full, refs)
			}
		}
	}
	return nil
}

func checkQueryAPI(repo *core.Repository, info *complexFixture) error {
	merge := core.NewCommitHash(info.merge)
	commit := repo.Commit(merge)
	if commit == nil || commit.CommitHash.Full != info.merge {
		return fmt.Errorf("commit query failed for %s", info.merge)
	}
	if len(repo.AllCommits()) == 0 {
		return fmt.Errorf("all_commits returned no commits")
	}
	parents := repo.ParentsHash(merge)
	if len(parents) != 2 {
		return fmt.Errorf("parents_hash for merge returned %v", parents)
	}
	if parents[0].Full != info.master || parents[1].Full != info.feature {
		return fmt.Errorf("parents_hash for merge did not retain first-parent order: %v", parents)
	}
	children := repo.ChildrenHash(core.NewCommitHash(info.root))
	if len(children) == 0 {
		return fmt.Errorf("children_hash for root returned no children")
	}
	order := map[string]int{}
	for index, loaded := range repo.AllCommits() {
		order[loaded.CommitHash.Full] = index
	}
	for _, loaded := range repo.AllCommits() {
		previous := -1
		for _, child := range repo.ChildrenHash(loaded.CommitHash) {
			position, ok := order[child.Full]
			if !ok {
				return fmt.Errorf("children_hash for %s returned unknown child %s", loaded.CommitHash.Full, child.Full)
			}
			if position < previous {
				return fmt.Errorf("children_hash for %s is not in repository commit order", loaded.CommitHash.Full)
			}
			previous = position
		}
	}
	if len(repo.Refs(merge)) == 0 {
		return fmt.Errorf("refs for merge returned none")
	}
	if len(repo.AllRefs()) == 0 {
		return fmt.Errorf("all_refs returned none")
	}
	if repo.Head().Kind != core.HeadKindBranch {
		return fmt.Errorf("head state = %+v", repo.Head())
	}
	detail, changes := repo.CommitDetail(core.NewCommitHash(info.root))
	if detail.CommitHash.Full != info.root || len(changes) == 0 {
		return fmt.Errorf("commit_detail for root returned %+v / %+v", detail, changes)
	}
	return nil
}

func checkEnumeration() error {
	f, _, err := makeBranchMergeFixture()
	if err != nil {
		return err
	}
	repo, err := core.Load(f.dir, core.SortChronological, nil, false)
	if err != nil {
		return err
	}
	for _, tc := range []struct {
		order string
		sort  core.SortCommit
	}{
		{"--date-order", core.SortChronological},
		{"--topo-order", core.SortTopological},
	} {
		loaded, err := core.Load(f.dir, tc.sort, nil, false)
		if err != nil {
			return err
		}
		expected, err := f.logHashes(tc.order)
		if err != nil {
			return err
		}
		actual := make([]string, 0, len(loaded.AllCommits()))
		for _, commit := range loaded.AllCommits() {
			actual = append(actual, commit.CommitHash.Full)
		}
		if !reflect.DeepEqual(actual, expected) {
			return fmt.Errorf("commit enumeration mismatch for %s\nwant %v\ngot  %v", tc.order, expected, actual)
		}
	}
	_ = repo
	return nil
}

func checkCommitDetail(repo *core.Repository, info *complexFixture) error {
	root, rootChanges := repo.CommitDetail(core.NewCommitHash(info.root))
	if root.CommitHash.Full != info.root || len(rootChanges) == 0 {
		return fmt.Errorf("root commit detail missing changes")
	}
	for _, change := range rootChanges {
		if change.Kind != core.FileChangeAdd {
			return fmt.Errorf("root commit change is not an addition: %+v", change)
		}
	}
	// A merge commit reports the changes introduced relative to its first
	// parent (the branch that was merged into), not a combined multi-parent
	// diff: only the file added on the merged-in branch is reported.
	merge, mergeChanges := repo.CommitDetail(core.NewCommitHash(info.merge))
	if merge.CommitHash.Full != info.merge {
		return fmt.Errorf("merge commit detail returned %+v", merge)
	}
	if len(mergeChanges) != 1 || mergeChanges[0].Kind != core.FileChangeAdd || mergeChanges[0].Path != "b.txt" {
		return fmt.Errorf("merge commit detail did not report the first-parent changes: %+v", mergeChanges)
	}
	return nil
}

func checkMailmap() error {
	f, hash, err := makeMailmapFixture()
	if err != nil {
		return err
	}
	enabled, err := core.Load(f.dir, core.SortChronological, nil, true)
	if err != nil {
		return err
	}
	commit := enabled.Commit(core.NewCommitHash(hash))
	if commit == nil || commit.AuthorName != "New Author" || commit.CommitterName != "New Committer" {
		return fmt.Errorf("mailmap identities were not applied: %+v", commit)
	}
	disabled, err := core.Load(f.dir, core.SortChronological, nil, false)
	if err != nil {
		return err
	}
	commit = disabled.Commit(core.NewCommitHash(hash))
	if commit == nil || commit.AuthorName != authorName || commit.CommitterName != committerName {
		return fmt.Errorf("raw identities were not used without mailmap: %+v", commit)
	}
	return nil
}

func checkCommitFields(repo *core.Repository, info *complexFixture) error {
	commit := repo.Commit(core.NewCommitHash(info.master))
	if commit == nil {
		return fmt.Errorf("master commit not found")
	}
	if commit.AuthorName == "" || commit.AuthorEmail == "" || commit.Subject == "" {
		return fmt.Errorf("commit fields are incomplete: %+v", commit)
	}
	if commit.AuthorDate.Format(time.RFC3339) != "2024-01-03T01:02:03-05:00" {
		return fmt.Errorf("author date lost offset: %s", commit.AuthorDate.Format(time.RFC3339))
	}
	if commit.CommitHash.Short != info.master[:7] {
		return fmt.Errorf("short hash = %q, want %q", commit.CommitHash.Short, info.master[:7])
	}
	return nil
}

func checkNoRustReferences(repoPath string) error {
	forbidden := []string{`"cargo"`, `"rustc"`, `serie-rs`, `libserie`}
	return scanGoFiles(repoPath, forbidden)
}

func checkNoCGOResidue(repoPath string) error {
	forbidden := []string{`import "C"`, `plugin.Open`, `syscall.LoadDLL`, `//go:linkname`, `net/rpc`, `grpc`}
	return scanGoFiles(repoPath, forbidden)
}

func scanGoFiles(repoPath string, forbidden []string) error {
	var files []string
	for _, root := range []string{filepath.Join(repoPath, "core"), filepath.Join(repoPath, "cmd")} {
		err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
			if err != nil {
				return err
			}
			if info.IsDir() {
				return nil
			}
			if strings.HasSuffix(path, ".go") && !strings.HasSuffix(path, "_test.go") {
				files = append(files, path)
			}
			return nil
		})
		if err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	sort.Strings(files)
	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			return err
		}
		for _, token := range forbidden {
			if bytes.Contains(data, []byte(token)) {
				return fmt.Errorf("production Go file %s contains forbidden residue %q", file, token)
			}
		}
	}
	return nil
}

func buildCLI(repoPath string) (string, error) {
	return buildCLIWithEnv(repoPath, nil)
}

func buildCLIWithEnv(repoPath string, extraEnv []string) (string, error) {
	dir, err := os.MkdirTemp("", "serie-cli-")
	if err != nil {
		return "", err
	}
	bin := filepath.Join(dir, "serie-core")
	goBin, err := exec.LookPath("go")
	if err != nil {
		return "", err
	}
	cmd := exec.Command(goBin, "build", "-buildvcs=false", "-o", bin, "./cmd/serie-core")
	cmd.Dir = repoPath
	cmd.Env = append(os.Environ(), "CGO_ENABLED=0", "GOTOOLCHAIN=local", "GOPROXY=off")
	cmd.Env = append(cmd.Env, extraEnv...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("go build ./cmd/serie-core: %v: %s", err, stderr.String())
	}
	return bin, nil
}

func runGoBuild(repoPath string, env []string) error {
	goBin, err := exec.LookPath("go")
	if err != nil {
		return err
	}
	cmd := exec.Command(goBin, "build", "-buildvcs=false", "./...")
	cmd.Dir = repoPath
	cmd.Env = append(os.Environ(), env...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("go build ./... failed: %v: %s", err, stderr.String())
	}
	return nil
}

func runGoTest(repoPath string, env []string) error {
	goBin, err := exec.LookPath("go")
	if err != nil {
		return err
	}
	// Compile all delivered tests offline. Their authored expectations are
	// diagnostic; evaluator-owned probes assert the required runtime behavior.
	cmd := exec.Command(goBin, "test", "-buildvcs=false", "-run", "^$", "./...")
	cmd.Dir = repoPath
	cmd.Env = append(os.Environ(), env...)
	var combined bytes.Buffer
	cmd.Stdout = &combined
	cmd.Stderr = &combined
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("go test ./... failed without Rust toolchain: %v: %s", err, combined.String())
	}
	return nil
}

func makeGitWrapper() (string, string, []string, error) {
	realGit, err := exec.LookPath("git")
	if err != nil {
		return "", "", nil, err
	}
	dir, err := os.MkdirTemp("", "serie-git-wrapper-")
	if err != nil {
		return "", "", nil, err
	}
	logPath := filepath.Join(dir, "git-argv.log")
	script := "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + shellQuote(logPath) + "\nexec " + shellQuote(realGit) + " \"$@\"\n"
	if err := os.WriteFile(filepath.Join(dir, "git"), []byte(script), 0o755); err != nil {
		return "", "", nil, err
	}
	env := []string{
		"PATH=" + dir + string(os.PathListSeparator) + os.Getenv("PATH"),
		"SERIE_GIT_LOG=" + logPath,
	}
	return dir, logPath, env, nil
}

func sanitizedPATH() (string, error) {
	dir, err := os.MkdirTemp("", "serie-bin-")
	if err != nil {
		return "", err
	}
	realGit, err := exec.LookPath("git")
	if err != nil {
		return "", err
	}
	if err := os.Symlink(realGit, filepath.Join(dir, "git")); err != nil {
		return "", err
	}
	return dir, nil
}

type sanitized struct {
	path string
	vars []string
}

func sanitizedEnv() (*sanitized, error) {
	dir, err := sanitizedPATH()
	if err != nil {
		return nil, err
	}
	return &sanitized{path: dir, vars: []string{
		"PATH=" + dir,
		"HOME=" + dir,
		"GIT_CONFIG_NOSYSTEM=1",
	}}, nil
}

func runCommand(bin string, args, extraEnv []string) (string, string, error) {
	cmd := exec.Command(bin, args...)
	cmd.Env = append(os.Environ(), extraEnv...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	return stdout.String(), stderr.String(), err
}

func parseJSONObject(data string) (map[string]any, error) {
	var object map[string]any
	if err := json.Unmarshal([]byte(data), &object); err != nil {
		return nil, fmt.Errorf("CLI output is not a JSON object: %v: %s", err, data)
	}
	return object, nil
}

func hasNonEmptyArray(object map[string]any, key string) bool {
	value, ok := object[key]
	if !ok {
		return false
	}
	array, ok := value.([]any)
	return ok && len(array) > 0
}

func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\\''") + "'"
}

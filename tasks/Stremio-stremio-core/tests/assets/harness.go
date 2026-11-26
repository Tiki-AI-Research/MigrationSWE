// Command harness is the BATCH-INTEGRATION-0001 target-side shared harness.
//
// It is created only by the INTEGRATION batch. It consumes the shared
// observation inventory (shared/integration/observations.json), dispatches the
// executable projections contributed by the P2P / F2P / DIFFERENTIAL / STATE
// batches, and writes a migration-observations:v6 report covering every
// required observation on the target side.
//
// It must not read dependencies/ or fragments/ (authoring-only trees) nor any
// authoring-private path. The asset root is derived from this file's location;
// the repository under test is discovered only from the documented environment
// keys and from the documented candidate mounts.
//
// Status semantics (never role-hardcoded):
//   - P2P / DIFFERENTIAL / STATE are pass-to-pass observations, observed by
//     executing the P2P batch's own projection against the repository, with the
//     repository's own Go build/test suite as the fallback probe.
//   - F2P is the target-only postcondition: the frozen target technology
//     (Go 1.23.0) must be in effect on the real build/execution path, and the
//     production code must not delegate back to the Rust engine. The F2P
//     batch's executable scope predicate is adopted when present.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

const (
	reportSchemaVersion = "migration-observations:v6"
	p2pBatchRunner      = "target/batches/BATCH-P2P-0001/run-p2p.sh"
	f2pScopeProbe       = "target/batches/BATCH-F2P-0001/dependency_scope_probe.sh"
	inventoryPath       = "shared/integration/observations.json"
)

var repoKeys = []string{"MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO"}

type inventoryItem struct {
	ID      string `json:"id"`
	Kind    string `json:"kind"`
	BatchID string `json:"batch_id"`
}

type valueEnvelope struct {
	Type  string      `json:"type"`
	Value interface{} `json:"value"`
}

type observation struct {
	ID     string        `json:"id"`
	Kind   string        `json:"kind"`
	Status string        `json:"status"`
	Value  valueEnvelope `json:"value"`
}

type report struct {
	SchemaVersion string        `json:"schema_version"`
	Observations  []observation `json:"observations"`
}

func main() {
	reportPath := "/logs/result.json"
	if len(os.Args) > 1 && os.Args[1] != "" {
		reportPath = os.Args[1]
	}

	assetsRoot := assetRoot()
	repo := repoRoot()

	items, err := readInventory(filepath.Join(assetsRoot, inventoryPath))
	if err != nil {
		fmt.Fprintf(os.Stderr, "harness: inventory: %v\n", err)
		os.Exit(1)
	}

	base := baseStatus(assetsRoot, repo)
	f2p := f2pStatus(assetsRoot, repo)

	rep := report{SchemaVersion: reportSchemaVersion, Observations: make([]observation, 0, len(items))}
	for _, it := range items {
		status := base
		projection := "status-wrapper"
		if strings.EqualFold(it.Kind, "F2P") {
			status = f2p
			projection = "target-technology-predicate"
		}
		rep.Observations = append(rep.Observations, observation{
			ID:     it.ID,
			Kind:   it.Kind,
			Status: status,
			Value: valueEnvelope{
				Type:  "json",
				Value: map[string]string{"result": status, "projection": projection},
			},
		})
	}

	encoded, err := json.MarshalIndent(rep, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "harness: encode: %v\n", err)
		os.Exit(1)
	}
	encoded = append(encoded, '\n')
	if dir := filepath.Dir(reportPath); dir != "" {
		_ = os.MkdirAll(dir, 0o755)
	}
	if err := os.WriteFile(reportPath, encoded, 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "harness: write: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("target harness wrote %s (repo=%s base=%s f2p=%s, %d observations)\n", reportPath, repo, base, f2p, len(rep.Observations))
}

// assetRoot derives the asset root from this source file's location.
func assetRoot() string {
	if _, file, _, ok := runtime.Caller(0); ok {
		if abs, err := filepath.Abs(file); err == nil {
			return filepath.Dir(abs)
		}
		return filepath.Dir(file)
	}
	return "/assets"
}

// repoRoot discovers the repository under test only from the documented keys.
// An env-provided path is trusted only when it actually looks like a Go module;
// otherwise the documented candidate mounts are probed.
func repoRoot() string {
	for _, key := range repoKeys {
		if v := strings.TrimSpace(os.Getenv(key)); v != "" && isGoModule(v) {
			return v
		}
	}
	for _, candidate := range []string{"/workspace/repo", "/input/target", "/input/source"} {
		if isGoModule(candidate) {
			return candidate
		}
	}
	for _, key := range repoKeys {
		if v := strings.TrimSpace(os.Getenv(key)); v != "" {
			return v
		}
	}
	return "/workspace/repo"
}

func isGoModule(dir string) bool {
	return fileExists(filepath.Join(dir, "go.mod"))
}

func readInventory(path string) ([]inventoryItem, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var items []inventoryItem
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil, err
	}
	return items, nil
}

// baseStatus is the shared P2P / DIFFERENTIAL / STATE projection: the P2P batch
// executable projection is dispatched first when present so that batch-exec
// coverage of P2P is preserved; otherwise the repository's own Go test suite is
// the probe. The result is normalized to the stable status wrapper shared with
// the source side.
func baseStatus(assetsRoot, repo string) string {
	if !isGoModule(repo) {
		return "FAIL"
	}
	if result, ok := p2pBatchProjection(assetsRoot, repo); ok {
		return result
	}
	if !goBuilds(repo) {
		return "FAIL"
	}
	if runCommand(repo, "go", "test", "./...") != nil {
		return "FAIL"
	}
	return "PASS"
}

// p2pBatchProjection dispatches the P2P batch's own executable projection.
func p2pBatchProjection(assetsRoot, repo string) (string, bool) {
	script := filepath.Join(assetsRoot, p2pBatchRunner)
	if !fileExists(script) {
		return "", false
	}
	cmd := exec.Command("sh", script)
	cmd.Dir = repo
	cmd.Env = cleanEnv("REPO_ROOT="+repo, "P2P_LOG=/tmp/batch-p2p-0001-target.log")
	out, _ := cmd.Output()
	idx := strings.Index(string(out), `"projection":{"result":"`)
	if idx < 0 {
		return "", false
	}
	rest := string(out)[idx+len(`"projection":{"result":"`):]
	end := strings.Index(rest, `"`)
	if end < 0 {
		return "", false
	}
	return rest[:end], true
}

// f2pStatus is the target-only postcondition: the frozen target technology
// (Go 1.23.0) must be in effect on the real build/execution path and the
// production code must not delegate back to the Rust engine. When the F2P batch
// ships its executable scope predicate, that predicate is adopted as well.
func f2pStatus(assetsRoot, repo string) string {
	goMod, err := os.ReadFile(filepath.Join(repo, "go.mod"))
	if err != nil || !strings.Contains(string(goMod), "go 1.23") {
		return "FAIL"
	}
	if !goBuilds(repo) {
		return "FAIL"
	}
	if hasRustDelegation(repo) {
		return "FAIL"
	}
	if scopeProbe := filepath.Join(assetsRoot, f2pScopeProbe); fileExists(scopeProbe) {
		cmd := exec.Command("sh", scopeProbe, repo)
		cmd.Dir = repo
		cmd.Env = cleanEnv()
		if cmd.Run() != nil {
			return "FAIL"
		}
	}
	return "PASS"
}

func goBuilds(repo string) bool {
	cmd := exec.Command("go", "build", "./...")
	cmd.Dir = repo
	cmd.Env = cleanEnv()
	return cmd.Run() == nil
}

func runCommand(dir, name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Dir = dir
	cmd.Env = cleanEnv()
	return cmd.Run()
}

// cleanEnv returns the process environment with the documented repo-root keys
// removed, plus any explicit override entries. This keeps the child probes from
// inheriting a repo-root alias that does not belong to this side.
func cleanEnv(extra ...string) []string {
	blocked := map[string]bool{}
	for _, key := range repoKeys {
		blocked[key] = true
	}
	env := make([]string, 0, len(os.Environ())+len(extra))
	for _, kv := range os.Environ() {
		key := kv
		if i := strings.IndexByte(kv, '='); i >= 0 {
			key = kv[:i]
		}
		if blocked[key] {
			continue
		}
		env = append(env, kv)
	}
	return append(env, extra...)
}

// hasRustDelegation scans production Go sources for cgo / FFI / subprocess /
// RPC / dynamic-loading references to the Rust engine.
func hasRustDelegation(repo string) bool {
	forbidden := []string{
		`import "C"`,
		"//go:cgo",
		"#cgo",
		`"runtime/cgo"`,
		`"os/exec"`,
		`"plugin"`,
		`"net/rpc"`,
		"dlopen",
	}
	found := false
	_ = filepath.WalkDir(repo, func(path string, d os.DirEntry, err error) error {
		if err != nil || d == nil {
			return nil
		}
		if d.IsDir() {
			name := d.Name()
			if name == ".git" || name == "stremio-core-web" || name == "vendor" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(d.Name(), ".go") || strings.HasSuffix(d.Name(), "_test.go") {
			return nil
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		text := string(raw)
		for _, needle := range forbidden {
			if strings.Contains(text, needle) {
				found = true
				return filepath.SkipAll
			}
		}
		return nil
	})
	return found
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

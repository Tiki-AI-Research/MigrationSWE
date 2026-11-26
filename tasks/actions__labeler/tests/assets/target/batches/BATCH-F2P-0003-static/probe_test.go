// Package batchf2pstatic contains the target-technology postconditions owned by
// BATCH-F2P-0003 that are independent of the candidate's internal Go API shape.
//
// The dependency, entrypoint, import-policy and subprocess-policy predicates
// observe only repository-level facts (go.mod, action.yml, cmd/labeler,
// production Go imports).  They are intentionally evaluated in their own
// compilation unit so that a candidate whose internal package signatures differ
// from the reference is never rejected on technology grounds by an unrelated
// probe compilation failure.
package batchf2pstatic

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"
)

func moduleRoot(t *testing.T) string {
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
			t.Fatal("go.mod not found in any parent directory")
		}
		dir = parent
	}
}

func productionGoFiles(t *testing.T) []string {
	t.Helper()
	root := moduleRoot(t)
	var files []string
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			if info.Name() == ".git" || info.Name() == ".gomodcache" || info.Name() == ".gocache" || info.Name() == "node_modules" || info.Name() == "vendor" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		files = append(files, path)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return files
}

// F2P-057: the migrated action is a Go 1.24.0 module at the contract module
// path and declares no Node, @actions, or octokit dependency.  The instruction
// fixes the module path and technology, not any particular third-party library,
// so the predicate must not require reference-only dependencies.
func TestF2P057_GoModDependencyPolicy(t *testing.T) {
	root := moduleRoot(t)
	data, err := os.ReadFile(filepath.Join(root, "go.mod"))
	if err != nil {
		t.Fatal(err)
	}
	content := string(data)
	if !strings.Contains(content, "module github.com/actions/labeler") {
		t.Errorf("go.mod must declare module github.com/actions/labeler")
	}
	if !regexp.MustCompile(`(?m)^go\s+1\.24`).MatchString(content) {
		t.Errorf("go.mod must declare the Go 1.24 toolchain directive")
	}
	for _, forbidden := range []string{"@actions/", "@octokit/", "octokit", "node_modules"} {
		if strings.Contains(content, forbidden) {
			t.Errorf("go.mod declares forbidden Node dependency %q", forbidden)
		}
	}
}

// F2P-069: action.yml runs the migrated Go implementation as the action
// entrypoint.  The contract requires the Go implementation to be on the
// execution path; it does not prescribe a docker runtime, so a composite action
// that builds/runs the Go binary through a supporting script is equally valid.
// Parse the runtime declaration, not occurrences in comments or script text.
func TestF2P069_ActionRuntimeIsDocker(t *testing.T) {
	checkActionRuntime(t, true)
}

// The helper belongs to the evaluator and is materialized with the probes.
// It parses the runtime declaration and Docker artifact provenance; the full
// check executes the declared wrapper with a caller-local configuration/API.
func checkActionRuntime(t *testing.T, metadataOnly bool) {
	t.Helper()
	root := moduleRoot(t)
	args := []string{filepath.Join(root, "blackbox", "action_runtime.py"), "--repo", root}
	if metadataOnly {
		args = append(args, "--metadata-only")
	}
	command := exec.Command("python3", args...)
	command.Dir = root
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("declared action runtime does not preserve the Go action contract: %v\n%s", err, output)
	}
}

// F2P-070: the action entrypoint is the compiled Go binary built from
// cmd/labeler. Go does not prescribe the filename containing main.
// The contract requires the Node entrypoint to be gone
// from the action runtime; it does not require the out-of-scope dist bundle to
// be deleted (constraint 5 lets repository files outside the migration scope
// remain), so the predicate checks the entrypoint wiring, not file presence.
func TestF2P070_GoBinaryIsEntrypoint(t *testing.T) {
	root := moduleRoot(t)
	binary := filepath.Join(t.TempDir(), "labeler")
	command := exec.Command("go", "build", "-o", binary, "./cmd/labeler")
	command.Dir = root
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("cmd/labeler must build as a Go executable: %v\n%s", err, output)
	}
	info, err := os.Stat(binary)
	if err != nil || info.IsDir() || info.Mode()&0111 == 0 {
		t.Fatalf("cmd/labeler did not produce an executable: %v", err)
	}
	checkActionRuntime(t, false)
}

// F2P-071: no production Go source imports the TypeScript sources or @actions/*
// Node packages for the migrated responsibilities.
func TestF2P071_NoTypeScriptOrActionsImports(t *testing.T) {
	for _, path := range productionGoFiles(t) {
		file, err := parser.ParseFile(token.NewFileSet(), path, nil, parser.ImportsOnly)
		if err != nil {
			t.Fatal(err)
		}
		for _, spec := range file.Imports {
			name, _ := strconv.Unquote(spec.Path.Value)
			if strings.HasPrefix(name, "@actions/") || strings.HasPrefix(name, "@octokit/") || strings.HasPrefix(name, "github.com/actions/labeler/src") {
				t.Errorf("%s imports TypeScript/Node dependency %q", path, name)
			}
		}
	}
}

// F2P-072: no production Go source delegates the migrated responsibilities to
// Node.js through a subprocess, RPC, FFI, or dynamic loading.
func TestF2P072_NoSubprocessDelegation(t *testing.T) {
	for _, path := range productionGoFiles(t) {
		file, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		imports := map[string]string{}
		for _, spec := range file.Imports {
			name, _ := strconv.Unquote(spec.Path.Value)
			alias := filepath.Base(name)
			if spec.Name != nil {
				alias = spec.Name.Name
			}
			imports[alias] = name
		}
		ast.Inspect(file, func(node ast.Node) bool {
			call, ok := node.(*ast.CallExpr)
			if !ok || len(call.Args) == 0 {
				return true
			}
			module, name := "", ""
			switch fn := call.Fun.(type) {
			case *ast.SelectorExpr:
				if ident, ok := fn.X.(*ast.Ident); ok {
					module, name = imports[ident.Name], fn.Sel.Name
				}
			case *ast.Ident:
				module, name = imports["."], fn.Name
			}
			sink := module == "os/exec" && (name == "Command" || name == "CommandContext") ||
				module == "os" && name == "StartProcess" ||
				module == "syscall" && (name == "Exec" || name == "ForkExec")
			if !sink {
				return true
			}
			argument := 0
			if module == "os/exec" && name == "CommandContext" {
				argument = 1
			}
			if argument >= len(call.Args) {
				return true
			}
			command, known := constantString(call.Args[argument])
			if !known {
				return true
			}
			if sourceRuntimeCommand(command) {
				t.Errorf("%s invokes the TypeScript/Node runtime %q", path, command)
			}
			// Interpret only this invocation's argv, never all words near a
			// shell sink. The same parser is used by the integrity detector.
			base := filepath.Base(command)
			if base == "sh" || base == "bash" || base == "dash" || base == "env" {
				argv := []string{command}
				for _, arg := range call.Args[argument+1:] {
					value, known := constantString(arg)
					if !known {
						return true // dynamic expressions have independent integrity checks
					}
					argv = append(argv, value)
				}
				encoded, _ := json.Marshal(argv)
				check := exec.Command("python3", filepath.Join(moduleRoot(t), "blackbox", "process_commands.py"), "--argv", string(encoded))
				if output, err := check.CombinedOutput(); err != nil {
					t.Errorf("%s: process command policy: %v: %s", path, err, output)
				}
			}
			return true
		})
	}
}

func constantString(expr ast.Expr) (string, bool) {
	switch value := expr.(type) {
	case *ast.BasicLit:
		if value.Kind == token.STRING {
			text, err := strconv.Unquote(value.Value)
			return text, err == nil
		}
	case *ast.BinaryExpr:
		if value.Op == token.ADD {
			left, leftKnown := constantString(value.X)
			right, rightKnown := constantString(value.Y)
			return left + right, leftKnown && rightKnown
		}
	case *ast.ParenExpr:
		return constantString(value.X)
	}
	return "", false
}

func sourceRuntimeCommand(command string) bool {
	base := strings.ToLower(filepath.Base(command))
	return base == "node" || base == "nodejs" || base == "npx" || base == "ts-node" || base == "tsx" || base == "deno" || base == "bun" ||
		strings.HasSuffix(command, "dist/index.js")
}

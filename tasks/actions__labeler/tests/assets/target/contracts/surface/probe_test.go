// Package surface imports only the public packages named in instruction.md.
// Keep it separate from the YAML tests so an API defect does not fail them.
package surface

import (
	"go/ast"
	"go/build"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"testing"

	// Import real packages to require compilation without imposing declaration
	// kinds or signatures that instruction.md does not publish.
	_ "github.com/actions/labeler/internal/config"
	_ "github.com/actions/labeler/internal/core"
	_ "github.com/actions/labeler/internal/event"
	_ "github.com/actions/labeler/internal/githubapi"
	_ "github.com/actions/labeler/internal/inputs"
	_ "github.com/actions/labeler/internal/labeler"
	_ "github.com/actions/labeler/internal/match"
	_ "github.com/actions/labeler/internal/minimatch"
)

func repositoryRoot(t *testing.T) string {
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
			t.Fatal("go.mod not found")
		}
		dir = parent
	}
}

func exportedNames(dir string) (map[string]bool, error) {
	// ImportDir honors platform and file build constraints;
	// ignored files and *_test.go cannot supply a missing public identifier.
	pkg, err := build.Default.ImportDir(dir, 0)
	if err != nil {
		return nil, err
	}
	found := map[string]bool{}
	for _, name := range append(pkg.GoFiles, pkg.CgoFiles...) {
		file, err := parser.ParseFile(token.NewFileSet(), filepath.Join(dir, name), nil, 0)
		if err != nil {
			return nil, err
		}
		for _, declaration := range file.Decls {
			switch declaration := declaration.(type) {
			case *ast.FuncDecl:
				if declaration.Recv == nil && declaration.Name.IsExported() {
					found[declaration.Name.Name] = true
				}
			case *ast.GenDecl:
				for _, spec := range declaration.Specs {
					switch spec := spec.(type) {
					case *ast.TypeSpec:
						if spec.Name.IsExported() {
							found[spec.Name.Name] = true
						}
					case *ast.ValueSpec:
						for _, name := range spec.Names {
							if name.IsExported() {
								found[name.Name] = true
							}
						}
					}
				}
			}
		}
	}
	return found, nil
}

func TestGoPublicSurface(t *testing.T) {
	root := repositoryRoot(t)
	for path, names := range map[string][]string{
		"internal/config":    {"LabelConfigResult", "GetLabelConfigResultFromObject"},
		"internal/core":      {"Out", "Reset"},
		"internal/event":     {"Payload", "PullRequest", "Branch"},
		"internal/githubapi": {"Client", "NewClientForTest"},
		"internal/inputs":    {"Inputs", "EscapeNonPrintable"},
		"internal/labeler":   {"Labeler"},
		"internal/match":     {"BranchContext", "CheckAnyBranch"},
		"internal/minimatch": {"NewMatcher"},
	} {
		t.Run(path, func(t *testing.T) {
			found, err := exportedNames(filepath.Join(root, path))
			if err != nil {
				t.Fatal(err)
			}
			for _, name := range names {
				if !found[name] {
					t.Errorf("%s must export %s", path, name)
				}
			}
		})
	}
}

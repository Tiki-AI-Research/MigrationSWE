// Parse Go process boundaries without executing candidate code. Imported aliases,
// constant expressions, command variables and subpackages share the same checks.
package main

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

type values map[string][]string
type unit struct {
	file    *ast.File
	path    string
	imports map[string]string
}
type hit struct {
	Detector   string `json:"detector"`
	Capability string `json:"capability"`
	Path       string `json:"path"`
	Line       int    `json:"line"`
	Sink       string `json:"sink"`
	Token      string `json:"token"`
	Evidence   string `json:"evidence"`
}

func resolve(e ast.Expr, env values, imports map[string]string) []string {
	switch n := e.(type) {
	case *ast.BasicLit:
		if n.Kind == token.STRING {
			s, err := strconv.Unquote(n.Value)
			if err == nil {
				return []string{s}
			}
		}
	case *ast.Ident:
		if v, ok := env[n.Name]; ok {
			return v
		}
		if p, ok := imports[n.Name]; ok {
			return []string{p}
		}
	case *ast.ParenExpr:
		return resolve(n.X, env, imports)
	case *ast.UnaryExpr:
		return resolve(n.X, env, imports)
	case *ast.SelectorExpr:
		v := resolve(n.X, env, imports)
		if len(v) == 1 {
			return []string{v[0] + "." + n.Sel.Name}
		}
	case *ast.BinaryExpr:
		a, b := resolve(n.X, env, imports), resolve(n.Y, env, imports)
		if n.Op == token.ADD && len(a) == 1 && len(b) == 1 {
			return []string{a[0] + b[0]}
		}
	case *ast.CompositeLit:
		var out []string
		for _, e := range n.Elts {
			if kv, ok := e.(*ast.KeyValueExpr); ok {
				e = kv.Value
			}
			out = append(out, resolve(e, env, imports)...)
		}
		return out
	case *ast.CallExpr:
		f := resolve(n.Fun, env, imports)
		if len(f) == 1 {
			if (f[0] == "os/exec.LookPath" || f[0] == "string") && len(n.Args) > 0 {
				return resolve(n.Args[0], env, imports)
			}
			if f[0] == "path/filepath.Join" || f[0] == "path.Join" {
				var parts []string
				for _, a := range n.Args {
					v := resolve(a, env, imports)
					if len(v) != 1 {
						return nil
					}
					parts = append(parts, v[0])
				}
				return []string{strings.Join(parts, "/")}
			}
		}
	}
	return nil
}

func clone(e values) values {
	out := values{}
	for k, v := range e {
		out[k] = v
	}
	return out
}
func main() {
	if len(os.Args) != 3 {
		os.Exit(2)
	}
	root := os.Args[1]
	var tokens []string
	if json.Unmarshal([]byte(os.Args[2]), &tokens) != nil {
		os.Exit(2)
	}
	fset := token.NewFileSet()
	var units []unit
	globals := map[string]values{}
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.Type()&os.ModeSymlink != 0 {
			return nil
		}
		if d.IsDir() {
			switch d.Name() {
			case ".git", "node_modules", ".gomodcache", ".gocache", "vendor", "dist", "build":
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return err
		}
		imports := map[string]string{}
		for _, i := range f.Imports {
			p, _ := strconv.Unquote(i.Path.Value)
			name := filepath.Base(p)
			if i.Name != nil {
				name = i.Name.Name
			}
			imports[name] = p
		}
		units = append(units, unit{f, path, imports})
		return nil
	})
	if err != nil {
		os.Stderr.WriteString(err.Error())
		os.Exit(2)
	}
	// Resolve package constants to a fixed point, including references in other files.
	for pass := 0; pass < 8; pass++ {
		for _, u := range units {
			dir := filepath.Dir(u.path)
			if globals[dir] == nil {
				globals[dir] = values{}
			}
			env := globals[dir]
			for _, d := range u.file.Decls {
				if gd, ok := d.(*ast.GenDecl); ok {
					for _, s := range gd.Specs {
						if v, ok := s.(*ast.ValueSpec); ok {
							for i, n := range v.Names {
								if i < len(v.Values) {
									env[n.Name] = resolve(v.Values[i], env, u.imports)
								}
							}
						}
					}
				}
			}
		}
	}
	hits := []hit{}
	for _, u := range units {
		env := clone(globals[filepath.Dir(u.path)])
		ast.Inspect(u.file, func(n ast.Node) bool {
			if n == nil {
				return true
			}
			switch x := n.(type) {
			case *ast.FuncDecl:
				env = clone(globals[filepath.Dir(u.path)])
			case *ast.AssignStmt:
				for i, l := range x.Lhs {
					if name, ok := l.(*ast.Ident); ok {
						if i < len(x.Rhs) {
							env[name.Name] = resolve(x.Rhs[i], env, u.imports)
						} else {
							delete(env, name.Name)
						}
					}
				}
			case *ast.ValueSpec:
				for i, name := range x.Names {
					if i < len(x.Values) {
						env[name.Name] = resolve(x.Values[i], env, u.imports)
					}
				}
			}
			var sink string
			var args []ast.Expr
			var command []string
			switch x := n.(type) {
			case *ast.CallExpr:
				v := resolve(x.Fun, env, u.imports)
				if len(v) != 1 {
					return true
				}
				sink = v[0]
				args = x.Args
				index := 0
				switch sink {
				case "os/exec.CommandContext":
					index = 1
				case "os/exec.Command", "os.StartProcess", "syscall.Exec", "syscall.ForkExec", "syscall.StartProcess", "golang.org/x/sys/unix.Exec", "golang.org/x/sys/unix.ForkExec":
				default:
					return true
				}
				if len(args) <= index {
					return true
				}
				command = resolve(args[index], env, u.imports)
				if len(command) == 1 {
					base := strings.ToLower(filepath.Base(command[0]))
					if base == "sh" || base == "bash" || base == "dash" || base == "cmd" || base == "powershell" {
						for _, a := range args[index+1:] {
							command = append(command, resolve(a, env, u.imports)...)
						}
					}
				}
			case *ast.CompositeLit:
				v := resolve(x.Type, env, u.imports)
				if len(v) != 1 || v[0] != "os/exec.Cmd" {
					return true
				}
				sink = v[0]
				for _, e := range x.Elts {
					if kv, ok := e.(*ast.KeyValueExpr); ok {
						if key, ok := kv.Key.(*ast.Ident); ok && key.Name == "Path" {
							command = append(command, resolve(kv.Value, env, u.imports)...)
						}
					}
				}
			default:
				return true
			}
			for _, v := range command {
				for _, t := range tokens {
					pattern := `(?i)(^|[^a-z0-9_])` + regexp.QuoteMeta(t) + `(\.exe|\.cmd|\.bat)?($|[^a-z0-9_.])`
					if regexp.MustCompile(pattern).MatchString(v) {
						rel, _ := filepath.Rel(root, u.path)
						hits = append(hits, hit{"subprocess", "callgraph", filepath.ToSlash(rel), fset.Position(n.Pos()).Line, sink, t, strings.Join(command, " ")})
						return true
					}
				}
			}
			return true
		})
	}
	json.NewEncoder(os.Stdout).Encode(hits)
}

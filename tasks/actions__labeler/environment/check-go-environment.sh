#!/bin/sh
# Verify the candidate's compiler, cache permissions and offline dependencies.
set -eu

if [ "$(id -u)" -eq 0 ]; then
  echo 'Run this check as the agent user, not root.' >&2
  exit 1
fi
test "$(go env GOVERSION)" = go1.24.0
test "$(go env GOPROXY)" = off
test "$(go env GOSUMDB)" = off
test "$(go env GOTOOLCHAIN)" = local
test -w "$(go env GOMODCACHE)"
test -w "$(go env GOCACHE)"
command -v gofmt >/dev/null

check_dir=$(mktemp -d)
trap 'rm -rf "$check_dir"' EXIT HUP INT TERM
cd "$check_dir"
cat > offline_test.go <<'EOF'
package offlinecheck

import (
	"testing"

	"github.com/dlclark/regexp2"
	"gopkg.in/yaml.v3"
)

func TestOfflineDependencies(t *testing.T) {
	var config struct {
		HeadBranch string `yaml:"head-branch"`
	}
	if err := yaml.Unmarshal([]byte("head-branch: '^(?!release/)(?=feature/).*'\n"), &config); err != nil {
		t.Fatal(err)
	}
	pattern, err := regexp2.Compile(config.HeadBranch, regexp2.ECMAScript)
	if err != nil {
		t.Fatal(err)
	}
	for branch, want := range map[string]bool{
		"feature/offline": true,
		"release/v1":      false,
		"fix/bug":         false,
	} {
		got, err := pattern.MatchString(branch)
		if err != nil || got != want {
			t.Fatalf("branch %q: match=%v, error=%v; want %v", branch, got, err, want)
		}
	}
}
EOF

# Exercise both regex versions supported by the verifier, including module sums.
for regex_version in v1.11.5 v1.12.0; do
  cat > go.mod <<EOF
module offlinecheck

go 1.24.0

require (
  github.com/dlclark/regexp2 $regex_version
  gopkg.in/yaml.v3 v3.0.1
)
EOF
  rm -f go.sum
  go mod tidy
  go test -count=1 -timeout=60s ./...
done
echo 'Go 1.24.0 and offline regexp2/YAML dependencies are ready for the agent.'

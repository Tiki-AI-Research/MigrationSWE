# syntax=docker/dockerfile:1
FROM golang@sha256:b970e6d47c09fdd34179acef5c4fecaf6410f0b597a759733b3cbea04b4e604a

ENV GOTOOLCHAIN=local CGO_ENABLED=0

RUN apt-get update && apt-get install -y --no-install-recommends git python3 ca-certificates && rm -rf /var/lib/apt/lists/*

RUN GOBIN=/usr/local/bin GOMODCACHE=/tmp/go-tool-mod GOCACHE=/tmp/go-tool-cache GOPROXY=https://proxy.golang.org,direct go install gotest.tools/gotestsum@v1.12.0 && rm -rf /tmp/go-tool-mod /tmp/go-tool-cache && chmod 0755 /usr/local/bin/gotestsum

RUN cat > /usr/local/bin/serie-harness <<'PY'
#!/usr/bin/env python3
import os
import subprocess
import sys

assets = sys.argv[1] if len(sys.argv) > 1 else ''
report = sys.argv[2] if len(sys.argv) > 2 else ''
if not report:
    print('usage: serie-harness <assets-dir> <report-path>', file=sys.stderr)
    sys.exit(2)
os.makedirs(os.path.dirname(os.path.abspath(report)), exist_ok=True)
os.chdir('/workspace/repo')
names = ('harness.py', 'target/runner.py', 'target/harness.py', 'run_tests.py', 'runner.py', 'runner', 'harness', 'target/runner', 'target/harness', 'harness.sh', 'target/runner.sh', 'target/harness.sh', 'run_tests.sh')
for name in names:
    if not assets or not os.path.isdir(assets):
        break
    path = os.path.join(assets, name)
    if not os.path.isfile(path):
        continue
    if path.endswith('.py'):
        command = [sys.executable, path, report]
    elif os.access(path, os.X_OK):
        command = [path, report]
    else:
        command = ['sh', path, report]
    status = subprocess.call(command)
    if os.path.exists(report) and os.path.getsize(report) > 0:
        sys.exit(status)
sys.exit(subprocess.call(['gotestsum', '--junitfile=' + report, '--format=standard-verbose', '--', './...']))
PY
RUN chmod 0755 /usr/local/bin/serie-harness

ENV GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly GOCACHE=/workspace/repo/.gocache GOMODCACHE=/workspace/repo/.gomodcache GOPATH=/tmp/gopath GOTELEMETRY=off

WORKDIR /workspace/repo

USER root
WORKDIR /app
COPY . /tests
RUN if ! command -v git >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then \
      if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y --no-install-recommends git ca-certificates python3 util-linux && rm -rf /var/lib/apt/lists/*; \
      elif command -v apk >/dev/null 2>&1; then apk add --no-cache git ca-certificates python3 util-linux; \
      else exit 1; fi; fi \
    && command -v python3 >/dev/null 2>&1 && command -v git >/dev/null 2>&1 \
    && chmod +x /tests/test.sh /tests/grader.py \
    && chmod +x /tests/assets/target/runner \
    && find /tests/assets -name '*.sh' -type f -exec chmod +x {} + \
    && mkdir -p /logs/verifier /logs/artifacts \
    && chmod 0755 /logs /logs/verifier /logs/artifacts
RUN useradd --system --uid 1000 --create-home --home-dir /opt/harbor-candidate harbor-candidate 2>/dev/null || true; \
    chmod -R go-rwx /tests; \
    chmod 0700 /tests; \
    chmod 0755 /tests/test.sh /tests/grader.py; \
    if command -v setpriv >/dev/null 2>&1; then \
      setpriv --reuid=1000 --regid=1000 --clear-groups test -r /tests/assets/assets-manifest.json && { echo 'candidate unexpectedly read evaluator assets' >&2; exit 1; }; \
      setpriv --reuid=1000 --regid=1000 --clear-groups sh -c 'touch /logs/candidate-probe' && { echo 'candidate unexpectedly wrote evaluator logs' >&2; exit 1; }; \
      true; \
    fi
RUN git init /workspace/repo \
    && git -C /workspace/repo fetch /tests/assets/build-deps/repo/base-repo.bundle HEAD \
    && git -C /workspace/repo checkout --detach FETCH_HEAD \
    && git -C /workspace/repo config core.hooksPath /dev/null \
    && git -C /workspace/repo reflog expire --expire=now --all \
    && git -C /workspace/repo gc --prune=now
ENTRYPOINT []
CMD ["/tests/test.sh"]

# Evaluator-owned report; keep candidate change-scope checks strict.
RUN git -C /workspace/repo rev-parse --git-dir >/dev/null 2>&1 && printf '%s\n' '/.harbor-result.json' >> /workspace/repo/.git/info/exclude

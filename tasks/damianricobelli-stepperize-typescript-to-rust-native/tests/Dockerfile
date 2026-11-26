FROM rust@sha256:a45bf1f5d9af0a23b26703b3500d70af1abff7f984a7abef5a104b42c02a292b

RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates && rm -rf /var/lib/apt/lists/*

# Migration verifier runtime: the graded commands (cargo build/test --offline)
# must not depend on the candidate's rust-toolchain.toml, and the runtime is
# network-isolated, so bake the standard components into the pinned toolchain and
# pin the toolchain explicitly.  Without the components a candidate that declares
# `components = ["rustfmt", "clippy"]` (a normal Rust convention) makes rustup try
# to sync the channel over the network and fail the build before any test runs.
ENV RUSTUP_TOOLCHAIN=1.83.0-x86_64-unknown-linux-gnu
RUN rustup component add rustfmt clippy \
    && rustfmt --version \
    && cargo clippy --version

RUN mkdir -p /opt/migration/dependencies

COPY <<'HARNESS' /opt/migration/dependencies/run_tests.py
#!/usr/bin/env python3
import os
import subprocess
import sys
import xml.sax.saxutils as sx


def main():
    if len(sys.argv) < 3:
        print("usage: run_tests.py <assets> <report>", file=sys.stderr)
        return 2
    report = sys.argv[2]
    repo = os.environ.get("MIGRATION_REPO", os.getcwd())
    try:
        proc = subprocess.run(
            ["cargo", "test", "--workspace", "--locked", "--offline"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=1500,
        )
        output = proc.stdout + proc.stderr
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        returncode = 1
    cases = []
    for line in output.splitlines():
        if not line.startswith("test "):
            continue
        parts = line.split(" ... ", 1)
        if len(parts) != 2:
            continue
        name = parts[0][len("test "):].strip()
        status = parts[1].strip()
        if status in ("ok", "FAILED", "ignored"):
            cases.append((name, status))
    failures = sum(1 for _, status in cases if status == "FAILED")
    out = ['<?xml version="1.0" encoding="UTF-8"?>']
    out.append('<testsuites tests="{}" failures="{}">'.format(len(cases), failures))
    out.append('<testsuite name="stepperize-core" tests="{}" failures="{}">'.format(len(cases), failures))
    for name, status in cases:
        out.append('<testcase classname="stepperize-core" name="{}">'.format(sx.escape(name)))
        if status == "FAILED":
            out.append('<failure message="test failed"/>')
        out.append("</testcase>")
    out.append("</testsuite></testsuites>")
    directory = os.path.dirname(os.path.abspath(report))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(report, "w", encoding="utf-8") as handle:
        handle.write(os.linesep.join(out) + os.linesep)
    return 0 if returncode == 0 else 1


sys.exit(main())
HARNESS

RUN chmod a+rx /opt/migration/dependencies/run_tests.py && chmod -R a+rX /opt/migration/dependencies

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
    && mkdir -p /logs/verifier /logs/artifacts \
    && chmod 0755 /logs /logs/verifier /logs/artifacts

RUN PYTHONPATH=/tests python3 -B /tests/verifier_selftest/test_security_regression.py

RUN useradd --system --uid 1001 --create-home --home-dir /opt/harbor-candidate harbor-candidate 2>/dev/null || true; \
    chmod -R go-rwx /tests; \
    chmod 0700 /tests; \
    chmod 0755 /tests/test.sh /tests/grader.py; \
    if command -v setpriv >/dev/null 2>&1; then \
      setpriv --reuid=1001 --regid=1001 --clear-groups test -r /tests/assets/assets-manifest.json && { echo 'candidate unexpectedly read evaluator assets' >&2; exit 1; }; \
      setpriv --reuid=1001 --regid=1001 --clear-groups sh -c 'touch /logs/candidate-probe' && { echo 'candidate unexpectedly wrote evaluator logs' >&2; exit 1; }; \
      true; \
    fi
RUN git init /workspace/repo \
    && git -C /workspace/repo fetch /tests/assets/build-deps/repo/base-repo.bundle HEAD \
    && git -C /workspace/repo checkout --detach FETCH_HEAD \
    && git -C /workspace/repo config core.hooksPath /dev/null \
    && git -C /workspace/repo reflog expire --expire=now --all \
    && git -C /workspace/repo gc --prune=now \
    && git -C /workspace/repo rev-parse --git-dir >/dev/null 2>&1 \
    && printf '%s\n' '.harbor-result.json' '__pycache__/' '*.pyc' '*.pyo' '.pytest_cache/' '.mypy_cache/' '.ruff_cache/' '.coverage' '.cache/' >> /workspace/repo/.git/info/exclude
ENTRYPOINT []
CMD ["/tests/test.sh"]

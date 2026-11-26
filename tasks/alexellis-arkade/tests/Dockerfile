FROM golang@sha256:632dff7351799a5a9954a48dc06670ade72ee44e8c351cdc0e5a57af2fea21af

ENV CGO_ENABLED=0 \
    GOTOOLCHAIN=local

RUN GOBIN=/usr/local/bin go install gotest.tools/gotestsum@v1.13.0

WORKDIR /workspace/repo

USER root
WORKDIR /app
COPY . /tests
RUN set -eux; \
    # The evaluator image is pinned by digest and already ships every tool the
    # grader needs (Debian bookworm's python3, git, ca-certificates and
    # util-linux).  Installing them here would make the built image depend on a
    # *live* package repository, so a rebuild could silently change the
    # interpreter the scoring scripts run on.  Assert the pinned base instead of
    # installing: a future base that lacks one of these fails the build loudly.
    for tool in python3 git setpriv update-ca-certificates; do \
      command -v "$tool" >/dev/null 2>&1; \
    done; \
    chmod +x /tests/test.sh /tests/grader.py \
    && chmod 0755 /tests/assets/target/runner \
    && test -x /tests/assets/target/runner \
    && mkdir -p /logs/verifier /logs/artifacts \
    && chmod 0755 /logs /logs/verifier /logs/artifacts \
    && cd /tests && PYTHONDONTWRITEBYTECODE=1 python3 -c \
         'from verifier.security_selfcheck import run_security_selfcheck; run_security_selfcheck()' \
    && PYTHONDONTWRITEBYTECODE=1 python3 -c \
         'from verifier.verifier_engine import validate_weights; import json; validate_weights(json.load(open("/tests/config.json"))["weights"])' \
    && PYTHONDONTWRITEBYTECODE=1 python3 -c \
         'from verifier.integrity import validate_public_integrity, validate_public_projection_contract; from pathlib import Path; import json; root = Path("/tests"); validate_public_integrity(root); validate_public_projection_contract(root, json.loads((root / "config.json").read_text(encoding="utf-8"))["public_contract"])'
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
    && test "$(git -C /workspace/repo rev-parse FETCH_HEAD)" = "0a0a800fd7554d4eddb1856f9ef8a21214e95bab" \
    && git -C /workspace/repo checkout --detach FETCH_HEAD \
    && git -C /workspace/repo config core.hooksPath /dev/null \
    && git -C /workspace/repo reflog expire --expire=now --all \
    && git -C /workspace/repo gc --prune=now
ENTRYPOINT []
CMD ["/tests/test.sh"]

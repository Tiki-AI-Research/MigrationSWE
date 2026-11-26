FROM docker.io/tikiaiteam/migration:versatica-jssip-verifier-base-image-20261126@sha256:ec3c94bfa1038f9ab5f1b270e43431346474864cc7c5c24e0aacfdadabe11374
COPY . /tests
RUN chmod +x /tests/test.sh /tests/grader.py \
    /tests/verifier/verifier.py /tests/verifier/hardgate.py \
    /tests/verifier/sandbox.py /tests/verifier/asset_integrity.py \
    /tests/verifier/test_security_selfcheck.py \
    /tests/verifier/protected_paths.py

# The hidden harness and the grammar rebuild run as an unprivileged identity.
# Evaluator-owned assets and verifier source stay root-only.
RUN (getent passwd 1001 >/dev/null 2>&1 || useradd --system --uid 1001 \
        --create-home --home-dir /home/harbor-candidate harbor-candidate) \
    && chmod -R go-rwx /tests \
    && mkdir -p /logs/verifier /logs/artifacts \
    && chmod 700 /logs /logs/verifier /logs/artifacts


# The separate verifier image is based on the frozen environment snapshot.  It
# must carry the target generator in the repository dependency tree as well:
# the project's own `npm test`/grammar build is executed during behavior and
# provenance checks, and the verifier has no network at that point.
RUN cd /workspace/repo \
    && npm install --no-save --no-package-lock --ignore-scripts peggy@4.2.0 \
    && test -x /workspace/repo/node_modules/.bin/peggy \
    && find /workspace/repo/node_modules -type d -name pegjs -prune -exec rm -rf {} + \
    && rm -f /workspace/repo/node_modules/.bin/pegjs \
    && ! node -e "require.resolve('pegjs')"

# Ship the target generator with the verifier so that parser provenance can be
# proven at evaluation time without trusting the candidate dependency tree.
# Without this the provenance check cannot run and a fake migration could pass.
# ``esbuild`` is imported by the project build script, so it is shipped too:
# ``node_modules`` is not tracked by git and the verification workspace may
# carry no (or a pruned) dependency tree.
RUN mkdir -p /opt/verifier-tools \
    && cd /opt/verifier-tools \
    && npm init -y >/dev/null 2>&1 \
    && npm install --no-audit --no-fund --ignore-scripts peggy@4.2.0 \
    && npm install --no-audit --no-fund esbuild@0.27.2 \
    && test -x /opt/verifier-tools/node_modules/.bin/peggy

# Build-time regression suite: every supplied attack class must fail and every
# legitimate control (build-time spawn helper, production comment mentioning
# skip) must pass.  The same suite runs again at scoring startup in --quick mode.
RUN python3 /tests/verifier/test_security_selfcheck.py --full

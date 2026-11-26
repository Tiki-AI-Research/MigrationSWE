FROM node@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0

ENV npm_config_update_notifier=false
ENV npm_config_fund=false

WORKDIR /opt/migration/dependencies

RUN cat > package.json <<'EOF'
{
  "name": "migration-target-dependencies",
  "version": "1.0.0",
  "private": true,
  "dependencies": {
    "@emotion/react": "11.14.0",
    "@emotion/styled": "11.14.1",
    "@jest/globals": "29.7.0",
    "@mui/icons-material": "5.18.0",
    "@mui/material": "5.18.0",
    "@testing-library/react": "14.2.1",
    "@types/insane": "1.0.0",
    "@types/jest": "29.5.12",
    "@types/markdown-it": "14.1.1",
    "@types/react": "18.2.57",
    "@types/react-dom": "18.2.19",
    "@typescript-eslint/eslint-plugin": "6.21.0",
    "@typescript-eslint/parser": "6.21.0",
    "@vitejs/plugin-react-swc": "3.11.0",
    "eslint": "8.56.0",
    "eslint-plugin-react-hooks": "4.6.0",
    "eslint-plugin-react-refresh": "0.4.26",
    "eslint-plugin-simple-import-sort": "11.0.0",
    "highlight.js": "11.11.1",
    "insane": "2.6.2",
    "jest": "29.7.0",
    "jest-environment-jsdom": "29.7.0",
    "jest-junit": "16.0.0",
    "markdown-it": "14.1.0",
    "prettier": "3.2.5",
    "react": "18.3.1",
    "react-colorful": "5.6.1",
    "react-dom": "18.3.1",
    "react-test-renderer": "18.2.0",
    "ts-jest": "29.1.2",
    "ts-node": "10.9.2",
    "tsup": "8.0.2",
    "typescript": "5.3.3",
    "vite": "5.4.21",
    "zod": "3.25.76",
    "zustand": "4.5.7"
  }
}
EOF

RUN npm install --no-audit --no-fund
RUN chmod -R a+rX /opt/migration/dependencies
RUN mkdir -p /opt/migration/dependencies/node_modules/@usewaypoint && ln -s ../../packages/block-avatar /opt/migration/dependencies/node_modules/@usewaypoint/block-avatar && ln -s ../../packages/block-button /opt/migration/dependencies/node_modules/@usewaypoint/block-button && ln -s ../../packages/block-columns-container /opt/migration/dependencies/node_modules/@usewaypoint/block-columns-container && ln -s ../../packages/block-container /opt/migration/dependencies/node_modules/@usewaypoint/block-container && ln -s ../../packages/block-divider /opt/migration/dependencies/node_modules/@usewaypoint/block-divider && ln -s ../../packages/block-heading /opt/migration/dependencies/node_modules/@usewaypoint/block-heading && ln -s ../../packages/block-html /opt/migration/dependencies/node_modules/@usewaypoint/block-html && ln -s ../../packages/block-image /opt/migration/dependencies/node_modules/@usewaypoint/block-image && ln -s ../../packages/block-spacer /opt/migration/dependencies/node_modules/@usewaypoint/block-spacer && ln -s ../../packages/block-text /opt/migration/dependencies/node_modules/@usewaypoint/block-text && ln -s ../../packages/document-core /opt/migration/dependencies/node_modules/@usewaypoint/document-core && ln -s ../../packages/email-builder /opt/migration/dependencies/node_modules/@usewaypoint/email-builder

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
RUN set -e; \
    if [ -e /opt/migration/dependencies/node_modules ]; then rm -rf /workspace/repo/node_modules && cp -a /opt/migration/dependencies/node_modules /workspace/repo/node_modules; fi; \
    true
ENTRYPOINT []
CMD ["/tests/test.sh"]

FROM scratch AS migrationrepositorycontext
ADD assets/build-deps/repo/build-context.tar /

FROM rust@sha256:0ff31c9ffa641a62e48d543fb00b4960955ea375f40776f40f585b89e654cc5e AS rust-toolchain
RUN rustup target add --toolchain 1.85.0 wasm32-unknown-unknown
FROM node@sha256:e7add2a490bb17d97b485b8641bd88455fff7839ade0f5eba493c3508124d413

ENV RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    PATH=/opt/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

COPY --from=rust-toolchain /usr/local/rustup /opt/rustup
COPY --from=rust-toolchain /usr/local/cargo /opt/cargo

RUN npm install --global pnpm@10.28.0 \
 && mkdir -p /opt/migration/dependencies /workspace/repo \
 && chmod -R a+rwX /opt/cargo /opt/rustup /opt/migration

WORKDIR /opt/build/repo
COPY --from=migrationrepositorycontext . .

RUN pnpm install --frozen-lockfile --store-dir /opt/migration/dependencies/pnpm-store

# Keep a trusted compiler for declaration/production-source checks. Candidate
# dependencies must not supply the parser that grades their own implementation.
RUN mkdir -p /opt/stepperize-verifier-node-modules \
 && cp -RL node_modules/typescript /opt/stepperize-verifier-node-modules/typescript \
 && chmod -R go-w /opt/stepperize-verifier-node-modules

# Candidate migrations may regenerate Cargo.lock while keeping the declared
# napi dependencies. Keep both the supplied lock and a freshly resolved lock
# available offline. The observed candidate needs regex 1.13.2 while the
# supplied lock has 1.13.1, so retain that exact version even if newer releases
# become available when this image is rebuilt.
RUN set -eux; \
    mkdir -p /tmp/cargo-vendor-current /tmp/cargo-vendor-regex-1.13.2; \
    cp packages/core/native/Cargo.toml /tmp/cargo-vendor-current/Cargo.toml; \
    cp packages/core/native/Cargo.toml /tmp/cargo-vendor-regex-1.13.2/Cargo.toml; \
    cp -a packages/core/native/src /tmp/cargo-vendor-current/src; \
    cp -a packages/core/native/src /tmp/cargo-vendor-regex-1.13.2/src; \
    cp packages/core/native/build.rs /tmp/cargo-vendor-current/build.rs; \
    cp packages/core/native/build.rs /tmp/cargo-vendor-regex-1.13.2/build.rs; \
    cp packages/core/native/Cargo.lock /tmp/cargo-vendor-regex-1.13.2/Cargo.lock; \
    cargo generate-lockfile --manifest-path /tmp/cargo-vendor-current/Cargo.toml; \
    cargo update --manifest-path /tmp/cargo-vendor-regex-1.13.2/Cargo.toml -p regex --precise 1.13.2; \
    cargo vendor --locked --versioned-dirs \
      --manifest-path packages/core/native/Cargo.toml \
      --sync /tmp/cargo-vendor-current/Cargo.toml \
      --sync /tmp/cargo-vendor-regex-1.13.2/Cargo.toml \
      /opt/migration/dependencies/cargo-vendor; \
    test -d /opt/migration/dependencies/cargo-vendor/regex-1.13.1; \
    test -d /opt/migration/dependencies/cargo-vendor/regex-1.13.2; \
    rm -rf /tmp/cargo-vendor-current /tmp/cargo-vendor-regex-1.13.2

# The offline pnpm store is consumed by the unprivileged candidate (uid 1000).
# pnpm hardlinks store files into node_modules and then chmods the bin shims: a
# chmod on a root-owned inode raises EPERM for uid 1000, so the store (and the
# shared cargo/rustup trees) must be owned by the candidate uid. chmod alone is
# not enough because ownership, not mode, drives chmod.
RUN mkdir -p /opt/migration/dependencies/cargo-config \
 && printf '%s\n' \
      '[source.crates-io]' \
      'replace-with = "vendored-sources"' \
      '[source.vendored-sources]' \
      'directory = "/workspace/repo/vendor"' \
      > /opt/migration/dependencies/cargo-config/config.toml \
 && chmod -R a+rwX /opt/cargo /opt/rustup /opt/migration/dependencies \
 && chown -R 1000:1000 /opt/migration/dependencies /opt/cargo /opt/rustup

RUN rm -rf /opt/build

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
RUN python3 -B /tests/verifier/reward_details_selftest.py
RUN python3 -B /tests/verifier/security_regression_selftest.py
RUN node /tests/assets/shared/native-layout.selftest.mjs
RUN node /tests/assets/shared/public-contract.selftest.mjs
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
    if [ -e /opt/migration/dependencies/cargo-vendor ]; then rm -rf /workspace/repo/vendor && cp -a /opt/migration/dependencies/cargo-vendor /workspace/repo/vendor; fi; \
    if [ -e /opt/migration/dependencies/cargo-config ]; then rm -rf /workspace/repo/.cargo && cp -a /opt/migration/dependencies/cargo-config /workspace/repo/.cargo; fi; \
    true
ENTRYPOINT []
CMD ["/tests/test.sh"]

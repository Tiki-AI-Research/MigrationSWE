# syntax=docker/dockerfile:1.7
FROM node@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6

RUN corepack enable && corepack prepare pnpm@10.33.4 --activate

RUN mkdir -p /opt/migration/dependencies/build

COPY dependencies/package.json dependencies/pnpm-lock.yaml /opt/migration/dependencies/build/
RUN printf 'onlyBuiltDependencies:\n  - esbuild\n' > /opt/migration/dependencies/build/pnpm-workspace.yaml \
    && cd /opt/migration/dependencies/build \
    && pnpm install --frozen-lockfile \
    && mv node_modules /opt/migration/dependencies/node_modules \
    && rm -rf /opt/migration/dependencies/build

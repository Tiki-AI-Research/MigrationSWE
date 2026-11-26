FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0

ARG TARGETARCH

ENV NPM_CONFIG_CACHE=/opt/npm-cache \
    NPM_CONFIG_PREFER_OFFLINE=true \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false

WORKDIR /workspace

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        patch \
        python3 \
        python3-pip \
        ripgrep && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir --break-system-packages mini-swe-agent

RUN python3 -m pip install --no-cache-dir --break-system-packages uv

COPY base-repo.tar.gz /tmp/base-repo.tar.gz
RUN printf '%s  %s\n' \
        '24a49ec5441a1d2e02c1d52d8e80e19f7ece4094509665a599dd040202489977' /tmp/base-repo.tar.gz | sha256sum -c - && \
    mkdir -p /workspace/repo && \
    tar -xzf /tmp/base-repo.tar.gz -C /workspace/repo && \
    rm -f /tmp/base-repo.tar.gz
COPY collect_candidate.py /workspace/collect_candidate.py
COPY task-contract.json /workspace/task-contract.json
RUN rm -rf /workspace/repo/.git && \
    git init -q /workspace/repo && \
    git -C /workspace/repo add -A && \
    git -C /workspace/repo -c user.email=eval@harbor.local -c user.name=harbor \
        commit -qm "baseline: eval snapshot before agent" && \
    test "$(git -C /workspace/repo rev-parse 'HEAD^{tree}')" = "6fba19571de6113b622e5fc1de1c25fc113b8df7" && \
    git -C /workspace/repo rev-parse HEAD > /workspace/repo/.base_commit && \
    chmod 0555 /workspace/collect_candidate.py && \
    chmod 0444 /workspace/task-contract.json && \
    printf '%s\n' '.base_commit' >> /workspace/repo/.git/info/exclude

ARG NPM_PREWARM_REV=20261027
COPY prewarm/ /opt/prewarm/
RUN echo "npm-prewarm-rev=$NPM_PREWARM_REV" && \
    cd /opt/prewarm && \
    npm ci --no-audit --no-fund && \
    mkdir -p /workspace/repo/node_modules && \
    cp -a /opt/prewarm/node_modules/. /workspace/repo/node_modules/

RUN (id -u node >/dev/null 2>&1 && usermod -l evaluator -d /home/evaluator -m node && groupmod -n evaluator node || useradd --create-home --uid 1000 evaluator) && \
    chown -R evaluator:evaluator /workspace /opt/npm-cache

# Preinstall Codex and Claude Code; Harbor supplies model configuration at runtime.
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && \
    rm -f /tmp/install_agents.sh && \
    chown -R evaluator:evaluator /opt/npm-cache

USER evaluator

ENV LITELLM_LOCAL_MODEL_COST_MAP=True \
    UV_CONSTRAINT=/home/evaluator/.uv-constraints.txt
RUN printf 'litellm==1.102.4\n' > /home/evaluator/.uv-constraints.txt && \
    chmod 0444 /home/evaluator/.uv-constraints.txt && \
    uv python install 3.12 && \
    uv tool install --python 3.12 mini-swe-agent --with litellm --with orjson --with fastapi && \
    mini-swe-agent --help >/dev/null

ENV PATH=/home/evaluator/.local/bin:$PATH

CMD ["sleep", "infinity"]

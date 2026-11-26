FROM docker.io/tikiaiteam/migration:linked-shiv-base-image-20261126@sha256:e1e2f4480d68315218f07b46acb896c5a10ba7c97f0d0b47ea97a4f1c0a50f65 AS shiv-base
FROM python@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b

RUN apt-get update \
    && apt-get install -y --no-install-recommends git iproute2 util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/migration/dependencies/venv

# Candidate processes run as an unprivileged identity after entering their
# private namespaces.  Keep the evaluator scratch and logs root-only; the
# verifier copies reports out after the candidate exits.
RUN useradd --system --uid 1001 --create-home --home-dir /home/harbor-candidate harbor-candidate \
    && mkdir -p /workspace/candidate-sandboxes /var/lib/harbor-evaluator /logs/verifier /logs/artifacts \
    && chmod 711 /workspace /workspace/candidate-sandboxes \
    && chmod 700 /var/lib/harbor-evaluator /logs /logs/verifier /logs/artifacts \
    && touch /var/lib/harbor-evaluator/protected-marker \
    && chmod 600 /var/lib/harbor-evaluator/protected-marker \
    && if setpriv --reuid=1001 --regid=1001 --clear-groups test -r /var/lib/harbor-evaluator/protected-marker; then \
         echo 'candidate unexpectedly read evaluator marker' >&2; exit 1; \
       fi \
    && if setpriv --reuid=1001 --regid=1001 --clear-groups sh -c 'touch /logs/verifier/candidate-probe'; then \
         echo 'candidate unexpectedly wrote evaluator logs' >&2; exit 1; \
       fi

RUN /opt/migration/dependencies/venv/bin/python -m pip install --no-cache-dir \
        "click==8.1.7" \
        "pip>=9.0.3" \
        "setuptools" \
        "wheel" \
        "coverage" \
        "pytest" \
        "mypy" \
        "flake8"

# Migration tests create an offline source venv with Click 6.7 and perform a
# PEP 517 editable install.  Keep only the required build wheels in the image
# so those installs work after the candidate network namespace is disconnected.
RUN mkdir -p /opt/migration/wheels \
    && /opt/migration/dependencies/venv/bin/python -m pip download --no-deps \
        --dest /opt/migration/wheels \
        "click==6.7" \
    && /opt/migration/dependencies/venv/bin/python -m pip download --no-deps \
        --dest /opt/migration/wheels \
        "click==8.1.7" \
    && /opt/migration/dependencies/venv/bin/python -m pip download --no-deps \
        --dest /opt/migration/wheels \
        "setuptools" \
        "wheel" \
        "packaging"

WORKDIR /workspace/repo

USER root
WORKDIR /app
COPY . /tests
COPY --from=shiv-base --chown=root:root /workspace/repo /app
# Fail the image build if the evidence -> hardgate -> reward-policy chain ever
# stops distinguishing L1/L2 cheating from an ordinary candidate failure.
RUN cd /tests && PYTHONDONTWRITEBYTECODE=1 python3 -m verifier.security_regression
RUN command -v python3 >/dev/null 2>&1 && command -v git >/dev/null 2>&1 \
    && test "$(git -C /app rev-parse HEAD)" = "ff542cbe75ea832df3a989d07c7fdf5214f727fa" \
    && test -z "$(git -C /app status --porcelain)" \
    && chmod +x /tests/test.sh /tests/grader.py \
    && chmod -R go-rwx /tests \
    && mkdir -p /logs/verifier /logs/artifacts \
    && chmod 700 /logs /logs/verifier /logs/artifacts \
    && if setpriv --reuid=1001 --regid=1001 --clear-groups test -r /tests/config.json; then \
         echo 'candidate unexpectedly read evaluator config' >&2; exit 1; \
       fi \
    && if setpriv --reuid=1001 --regid=1001 --clear-groups test -r /tests/verifier/harbor_entry.py; then \
         echo 'candidate unexpectedly read verifier source' >&2; exit 1; \
       fi
ENTRYPOINT []
CMD ["/tests/test.sh"]

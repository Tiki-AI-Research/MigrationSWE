FROM python@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b

RUN apt-get update \
    && apt-get install -y --no-install-recommends git iproute2 util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/migration/dependencies/venv

RUN /opt/migration/dependencies/venv/bin/python -m pip install --no-cache-dir \
        "click==8.1.7" \
        "pip>=9.0.3" \
        "setuptools" \
        "wheel" \
        "coverage" \
        "pytest" \
        "mypy" \
        "flake8"

WORKDIR /workspace/repo

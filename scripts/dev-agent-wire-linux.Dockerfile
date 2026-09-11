# Local Linux lane for the Controller/Spark wire-contract tier.
#
# The probe executables are built and *executed* by the pytest suites, so this
# image carries the same three tools the ``controller-spark-wire`` CI job
# installs on ubuntu-24.04: Python 3.12, uv, and the pinned Rust toolchain.
# ``scripts/dev_agent_wire_linux.py`` builds it with the versions asserted
# against .github/workflows/ci.yml; the defaults here are a fallback only.
# The base is pinned by tag plus index digest, never a moving tag alone; the
# digest is asserted against scripts/dev_agent_wire_linux.py. Refresh with
# `docker buildx imagetools inspect ubuntu:24.04` and a reviewed edit here, in
# the module, and in docs/local-linux-lane.md.
FROM ubuntu:24.04@sha256:224a1869083a311ef3f13648a154ba79832fbef6364d31493642ca03082da254

ARG RUST_TOOLCHAIN=1.97.1
ARG UV_VERSION=0.12.1
ARG PYTHON_VERSION=3.12

ENV DEBIAN_FRONTEND=noninteractive \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:/usr/local/bin:/usr/bin:/bin

# build-essential provides the C toolchain that ring and bundled rusqlite need.
# git is required by the lockfile's git dependency on the public contracts.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        pkg-config \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Fail the build rather than silently running a different Python than CI uses.
RUN python3 -c "import sys; assert '.'.join(map(str, sys.version_info[:2])) == '${PYTHON_VERSION}', sys.version"

RUN curl --proto '=https' --tlsv1.2 --silent --show-error --fail https://sh.rustup.rs \
        | sh -s -- \
            --yes \
            --no-modify-path \
            --profile minimal \
            --default-toolchain "${RUST_TOOLCHAIN}" \
    && rustup component add --toolchain "${RUST_TOOLCHAIN}" rustfmt \
    && chmod --recursive a+rwX /usr/local/rustup /usr/local/cargo

# The lane runs as the host uid, so rustup must not need to write a temp file
# under root-owned RUSTUP_HOME. Naming the installed toolchain directly and
# forbidding auto-install keeps a run from reaching the network for it.
ENV RUSTUP_TOOLCHAIN=${RUST_TOOLCHAIN} \
    RUSTUP_AUTO_INSTALL=0

RUN curl --location --silent --show-error --fail \
        "https://astral.sh/uv/${UV_VERSION}/install.sh" \
        | env UV_UNMANAGED_INSTALL=/usr/local/bin sh

# Resolve the interpreter from the image instead of downloading a managed
# CPython, so a run cannot silently drift away from the pinned 3.12.
ENV UV_PYTHON=${PYTHON_VERSION} \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

LABEL org.opencontainers.image.title="vonk-forge agent wire-contract lane" \
      org.opencontainers.image.description="Local Linux runner for control/tests/test_*_wire_bridge.py"

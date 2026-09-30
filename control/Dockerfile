# syntax=docker/dockerfile:1.7
# First-party language bases track their rolling tag so a rebuild picks up
# upstream patch releases. They hold no operator data, run only our own code,
# and are rebuilt through CI on every change; see docs/image-pin-refresh.md.
# Third-party bases below stay digest-pinned.
ARG NODE_IMAGE=node:26-trixie-slim
ARG PYTHON_IMAGE=python:3.14-slim-trixie
ARG SKOPEO_IMAGE=quay.io/skopeo/stable:v1.22.3-immutable@sha256:c0ee1f4edca5c01cb8d5611124f92f3cc47196ecab68aee5d0f90834e00574d5
ARG SKOPEO_INDEX_DIGEST=sha256:c0ee1f4edca5c01cb8d5611124f92f3cc47196ecab68aee5d0f90834e00574d5
ARG SKOPEO_AMD64_DIGEST=sha256:ab4c269c9e2bd11affe2666b860fb651a15afec121c15986b052b02e09d86239
ARG SKOPEO_ARM64_DIGEST=sha256:916612c4c9bcf1dd633137ec4ce88987e368c16b3d611642006ca5996fe28c98

FROM ${SKOPEO_IMAGE} AS skopeo
ARG TARGETARCH
ARG SKOPEO_INDEX_DIGEST
ARG SKOPEO_AMD64_DIGEST
ARG SKOPEO_ARM64_DIGEST
# skopeo rejects a reference that carries both a tag and a digest, so this
# resolution check uses the bare repository plus the index digest. The tag is
# carried on the FROM above.
RUN test -x /usr/bin/skopeo \
    && /usr/bin/skopeo --version \
    && test -f /etc/containers/policy.json \
    && test -f /etc/containers/registries.conf \
    && test -f /etc/ssl/certs/ca-bundle.crt \
    && case "$TARGETARCH" in \
        amd64) expected_child="$SKOPEO_AMD64_DIGEST" ;; \
        arm64) expected_child="$SKOPEO_ARM64_DIGEST" ;; \
        *) echo "unsupported target architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && raw_index="$(/usr/bin/skopeo inspect --tls-verify=true --raw \
        "docker://quay.io/skopeo/stable@${SKOPEO_INDEX_DIGEST}")" \
    && manifest_entry="$(printf '%s' "$raw_index" | sed 's/},{/}\n{/g' | grep -F "\"digest\":\"$expected_child\"")" \
    && printf '%s' "$manifest_entry" | grep -F "\"architecture\":\"$TARGETARCH\"" >/dev/null \
    && mkdir -p /skopeo-runtime/lib \
    && for library in \
        /usr/lib64/libacl.so* \
        /usr/lib64/libassuan.so* \
        /usr/lib64/libattr.so* \
        /usr/lib64/libaudit.so* \
        /usr/lib64/libbz2.so* \
        /usr/lib64/libcap-ng.so* \
        /usr/lib64/libcrypt.so* \
        /usr/lib64/libeconf.so* \
        /usr/lib64/libgpg-error.so* \
        /usr/lib64/libgpgme.so* \
        /usr/lib64/libgcc_s.so* \
        /usr/lib64/libpcre2-8.so* \
        /usr/lib64/libpam.so* \
        /usr/lib64/libpam_misc.so* \
        /usr/lib64/libselinux.so* \
        /usr/lib64/libsemanage.so* \
        /usr/lib64/libsepol.so* \
        /usr/lib64/libsubid.so* \
        /usr/lib64/libsqlite3.so* \
        /usr/lib64/libc.so* \
        /usr/lib64/libdl.so* \
        /usr/lib64/libm.so* \
        /usr/lib64/libpthread.so* \
        /usr/lib64/libresolv.so* \
        /usr/lib64/librt.so* \
        /usr/lib64/libnss_dns.so* \
        /usr/lib64/libnss_files.so* \
        /usr/lib64/ld-linux-*.so* \
        /lib/ld-linux-*.so*; do \
        test -e "$library" && cp -aL "$library" /skopeo-runtime/lib/ || true; \
    done

FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY control/web/package.json control/web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY control/web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
# Layers are ordered from least to most frequently changing so an ordinary
# source change rebuilds only the last two steps: the pinned public-contracts
# wheel first (it depends only on the lock files), then the third-party
# dependencies (they depend only on pyproject.toml), then our own source.
COPY control/pyproject.toml ./
COPY control/uv.lock ./uv.lock
COPY control/packaging/public-contracts.lock ./packaging/public-contracts.lock
# The repository image build runs scripts/build-control-wheel before invoking
# Docker so this local dependency exists and has the digest pinned in uv.lock.
COPY inventory/wheels/vonk_agent_protocol-4.1.0-py3-none-any.whl /wheels/
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && public_contracts_revision="$(sed -n 's/^revision = "//p' packaging/public-contracts.lock | tr -d '"')" \
    && test "$(printf '%s' "$public_contracts_revision" | wc -c)" = 40 \
    && git clone --filter=blob:none --no-checkout \
        https://github.com/CarstVaartjes/vonk-forge-recipes.git /public-contracts \
    && git -C /public-contracts checkout --detach "$public_contracts_revision" \
    && python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels \
        /public-contracts/contracts \
    && python -c 'import hashlib, pathlib, tomllib; [package] = [p for p in tomllib.loads(pathlib.Path("uv.lock").read_text())["package"] if p["name"] == "vonk-agent-protocol"]; [wheel] = package["wheels"]; assert wheel["hash"] == "sha256:" + hashlib.sha256(pathlib.Path("/wheels/vonk_agent_protocol-4.1.0-py3-none-any.whl").read_bytes()).hexdigest()' \
    && test "$(sha256sum /wheels/vonk_forge_public_contracts-2.1.0-py3-none-any.whl | cut -d' ' -f1)" = "$(sed -n 's/^sha256 = "//p' packaging/public-contracts.lock | tr -d '"')" \
    && rm -rf /public-contracts /var/lib/apt/lists/*
# Third-party dependencies resolve to the newest allowed release, so this layer
# is only reused within one UTC day: CI passes the date, and a fix released
# upstream reaches the next day's image even when pyproject.toml is unchanged.
# Our own vonk-* packages are installed from local files below. These are
# transitive dependencies of the image, not requested ones, so the REQUESTED
# markers that -r adds are removed to keep the layer identical to a one-step
# install.
ARG DEPENDENCY_DAY=undated
COPY pyproject.toml /vonk-cluster-profiles/
RUN echo "dependency day: ${DEPENDENCY_DAY}" \
    && python -c 'import pathlib, tomllib; print("\n".join(d for path in ("pyproject.toml", "/vonk-cluster-profiles/pyproject.toml") for d in tomllib.loads(pathlib.Path(path).read_text())["project"]["dependencies"] if not d.lower().startswith("vonk-")))' > /tmp/third-party.txt \
    && python -m pip install --no-cache-dir --prefix=/install -r /tmp/third-party.txt \
    && find /install -name REQUESTED -delete \
    && find /install -name RECORD -exec sed -i '/\.dist-info\/REQUESTED,/d' {} +
COPY src/cluster_profiles /vonk-cluster-profiles/src/cluster_profiles
COPY tools/hatch_build.py /vonk-cluster-profiles/tools/hatch_build.py
COPY schemas/install-release-manifest.schema.json /vonk-cluster-profiles/schemas/install-release-manifest.schema.json
COPY install/installer-release-public.pem /vonk-cluster-profiles/install/installer-release-public.pem
COPY control/src ./src
COPY control/alembic.ini ./alembic.ini
COPY control/migrations ./migrations
COPY --from=web /web/dist ./src/vonk_control/web
# pip only sees packages already in /install when that directory is on the
# path, so the dependency layer above is reused instead of downloaded again.
RUN PYTHONPATH="$(python -c 'import sysconfig; print(sysconfig.get_path("purelib", vars={"base": "/install", "platbase": "/install"}))')" \
    python -m pip install --no-cache-dir --prefix=/install \
    /wheels/vonk_agent_protocol-4.1.0-py3-none-any.whl \
    /wheels/vonk_forge_public_contracts-2.1.0-py3-none-any.whl \
    /vonk-cluster-profiles .

FROM ${PYTHON_IMAGE} AS runtime-root
ARG TARGETARCH
ARG SKOPEO_INDEX_DIGEST
ARG SKOPEO_AMD64_DIGEST
ARG SKOPEO_ARM64_DIGEST
# The pinned skopeo image ships /etc/ssl/certs as a symlink into /etc/pki/tls,
# while the Python base has a real directory there; COPY cannot replace a
# directory with a symlink, so drop the base directory before the copy below.
# The runtime installs nothing, so the base image's pip (and the libraries it
# vendors) is removed rather than shipped. Debian security fixes for packages
# the base already carries (e.g. OpenSSL) are applied here, so a base image
# that lags a fixed release never ships a known, fixable vulnerability.
RUN apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall --yes --root-user-action=ignore pip \
    && groupadd --gid 10001 control \
    && useradd --uid 10001 --gid 10001 --no-create-home control \
    && install -d -o 10001 -g 10001 -m 0700 /tmp/control \
    && install -d -o 10001 -g 10001 -m 0700 /tmp/control-cache \
    && install -d -o 10001 -g 10001 -m 1777 /var/tmp \
    && rm -rf /etc/ssl/certs
COPY --from=build /install /usr/local
COPY --from=skopeo /usr/bin/skopeo /usr/local/lib/skopeo/skopeo.real
COPY --from=skopeo /skopeo-runtime/lib /usr/local/lib/skopeo
COPY --from=skopeo /etc/containers /etc/containers
COPY --from=skopeo /etc/pki /etc/pki
COPY --from=skopeo /etc/ssl /etc/ssl
COPY --from=skopeo /usr/share/pki /usr/share/pki
COPY --from=skopeo /usr/share/containers /usr/share/containers
RUN case "$TARGETARCH" in \
        amd64) skopeo_loader=ld-linux-x86-64.so.2 ;; \
        arm64) skopeo_loader=ld-linux-aarch64.so.1 ;; \
        *) echo "unsupported target architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && test -x "/usr/local/lib/skopeo/$skopeo_loader" \
    && printf '%s\n' \
        '#!/bin/sh' \
        'set -eu' \
        'exec /usr/local/lib/skopeo/'"$skopeo_loader"' --library-path /usr/local/lib/skopeo /usr/local/lib/skopeo/skopeo.real "$@"' \
        > /usr/bin/skopeo \
    && chmod 0755 /usr/bin/skopeo \
    && test -x /usr/local/lib/skopeo/skopeo.real \
    && test -x /usr/bin/skopeo \
    && /usr/bin/skopeo --version \
    && test -f /etc/containers/policy.json \
    && test -f /etc/ssl/certs/ca-bundle.crt \
    && test ! -e /var/run/docker.sock
LABEL io.vonk.skopeo.index-digest="${SKOPEO_INDEX_DIGEST}" \
      io.vonk.skopeo.amd64-digest="${SKOPEO_AMD64_DIGEST}" \
      io.vonk.skopeo.arm64-digest="${SKOPEO_ARM64_DIGEST}"
WORKDIR /srv/vonk-control

FROM runtime-root AS worker-root

FROM runtime-root AS api-root
RUN install -d -o 0 -g 0 -m 0700 /run/secrets
# Non-secret runtime configuration for the whole Compose application ships
# with the Controller release. The API pre-exec stages it into a shared volume
# on every start, so pulling a new image is enough to roll out a change.
COPY deploy/compose/Caddyfile deploy/compose/caddy/entrypoint.sh /usr/local/share/vonk-forge/runtime-assets/caddy/
COPY deploy/compose/postgres/ /usr/local/share/vonk-forge/runtime-assets/postgres/
COPY deploy/compose/litellm/bootstrap-config.json deploy/compose/litellm/entrypoint.sh deploy/compose/litellm/config_supervisor.py /usr/local/share/vonk-forge/runtime-assets/litellm/
COPY deploy/compose/prometheus/ /usr/local/share/vonk-forge/runtime-assets/prometheus/
COPY deploy/compose/grafana/ /usr/local/share/vonk-forge/runtime-assets/grafana/
COPY deploy/compose/registry/config.yml /usr/local/share/vonk-forge/runtime-assets/registry/
COPY deploy/compose/tailscale/configure.sh /usr/local/share/vonk-forge/runtime-assets/tailscale/
COPY deploy/compose/hermes-agent/provision-litellm-key.py /usr/local/share/vonk-forge/runtime-assets/hermes-agent/

FROM scratch AS worker
COPY --from=worker-root / /
WORKDIR /srv/vonk-control
ARG SKOPEO_INDEX_DIGEST
ARG SKOPEO_AMD64_DIGEST
ARG SKOPEO_ARM64_DIGEST
LABEL io.vonk.skopeo.index-digest="${SKOPEO_INDEX_DIGEST}" \
      io.vonk.skopeo.amd64-digest="${SKOPEO_AMD64_DIGEST}" \
      io.vonk.skopeo.arm64-digest="${SKOPEO_ARM64_DIGEST}"
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    HOME=/tmp/control \
    TMPDIR=/tmp/control \
    XDG_CACHE_HOME=/tmp/control-cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001

FROM scratch AS api
COPY --from=api-root / /
WORKDIR /srv/vonk-control
ARG SKOPEO_INDEX_DIGEST
ARG SKOPEO_AMD64_DIGEST
ARG SKOPEO_ARM64_DIGEST
LABEL io.vonk.skopeo.index-digest="${SKOPEO_INDEX_DIGEST}" \
      io.vonk.skopeo.amd64-digest="${SKOPEO_AMD64_DIGEST}" \
      io.vonk.skopeo.arm64-digest="${SKOPEO_ARM64_DIGEST}"
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    HOME=/tmp/control \
    TMPDIR=/tmp/control \
    XDG_CACHE_HOME=/tmp/control-cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 0:0
ENTRYPOINT ["python", "-m", "vonk_control.api_preexec"]
CMD ["python", "-m", "vonk_control.api"]

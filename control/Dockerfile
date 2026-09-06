# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:235600a8101ab264e117b1768e925532262668dc9b581ef1dd7d96ced463b8e7
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
ARG SKOPEO_IMAGE=quay.io/skopeo/stable@sha256:8d25aabcf965e267b6a6ad02ff8da5512f77de1490063625093ff564797e88bc
ARG SKOPEO_INDEX_DIGEST=sha256:8d25aabcf965e267b6a6ad02ff8da5512f77de1490063625093ff564797e88bc
ARG SKOPEO_AMD64_DIGEST=sha256:9f6b762ce968b90b509c3bfa58f3d7abdbd1e0789340989db1572a1156fef116
ARG SKOPEO_ARM64_DIGEST=sha256:75ab1f75f046d1597502a1ef838000d8462bfdf32976f2966658728a76853a49

FROM ${SKOPEO_IMAGE} AS skopeo
ARG TARGETARCH
ARG SKOPEO_INDEX_DIGEST
ARG SKOPEO_AMD64_DIGEST
ARG SKOPEO_ARM64_DIGEST
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
COPY pyproject.toml /vonk-cluster-profiles/
COPY src/cluster_profiles /vonk-cluster-profiles/src/cluster_profiles
COPY control/pyproject.toml ./
COPY control/packaging/public-contracts.lock ./packaging/public-contracts.lock
COPY control/src ./src
COPY control/alembic.ini ./alembic.ini
COPY control/migrations ./migrations
COPY schemas/global/catalog-entity-v1.schema.json /schemas/global/catalog-entity-v1.schema.json
COPY schemas/global/harness-evidence-v1.schema.json /schemas/global/harness-evidence-v1.schema.json
COPY schemas/global/recipe-v1.schema.json /schemas/global/recipe-v1.schema.json
COPY schemas/global/test-report-v1.schema.json /schemas/global/test-report-v1.schema.json
COPY config/execution-harnesses /config/execution-harnesses
COPY --from=web /web/dist ./src/vonk_control/web
COPY inventory/wheels/vonk_agent_protocol-2.2.0-py3-none-any.whl /wheels/
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && public_contracts_revision="$(sed -n 's/^revision = "//p' packaging/public-contracts.lock | tr -d '"')" \
    && test "$(printf '%s' "$public_contracts_revision" | wc -c)" = 40 \
    && git clone --filter=blob:none --no-checkout \
        https://github.com/CarstVaartjes/vonk-forge-recipes.git /public-contracts \
    && git -C /public-contracts checkout --detach "$public_contracts_revision" \
    && python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels \
        /public-contracts/contracts \
    && test "$(sha256sum /wheels/vonk_agent_protocol-2.2.0-py3-none-any.whl | cut -d' ' -f1)" = "65b8b1486d983dc87f0035edfbe33a12c1c54ad69b95ca15be785ba2b8214dff" \
    && test "$(sha256sum /wheels/vonk_forge_public_contracts-0.1.0-py3-none-any.whl | cut -d' ' -f1)" = "$(sed -n 's/^sha256 = "//p' packaging/public-contracts.lock | tr -d '"')" \
    && rm -rf /public-contracts /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --prefix=/install \
    /wheels/vonk_agent_protocol-2.2.0-py3-none-any.whl \
    /wheels/vonk_forge_public_contracts-0.1.0-py3-none-any.whl \
    /vonk-cluster-profiles .

FROM ${PYTHON_IMAGE} AS runtime-root
ARG TARGETARCH
ARG SKOPEO_INDEX_DIGEST
ARG SKOPEO_AMD64_DIGEST
ARG SKOPEO_ARM64_DIGEST
RUN groupadd --gid 10001 control \
    && useradd --uid 10001 --gid 10001 --no-create-home control \
    && install -d -o 10001 -g 10001 -m 0700 /tmp/control \
    && install -d -o 10001 -g 10001 -m 0700 /tmp/control-cache \
    && install -d -o 10001 -g 10001 -m 1777 /var/tmp
COPY --from=build /install /usr/local
COPY --from=build /config /usr/local/lib/config
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

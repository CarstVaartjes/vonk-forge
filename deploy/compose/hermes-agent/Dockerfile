ARG HERMES_IMAGE=nousresearch/hermes-agent:v2026.9.24@sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7
FROM ${HERMES_IMAGE} AS managed

COPY .tool-versions /tmp/.tool-versions
COPY deploy/compose/python-bootstrap.lock deploy/compose/uv-bootstrap.lock /tmp/
# Refresh system/Python bundles and keep native uv independent of venv repair.
# Bootstrap pip against only the refreshed OS bundle; its old native-trust
# mode would otherwise add the vendored roots back to that store.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m ensurepip \
    && PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
        VONK_UV_VERSION="$(awk '$1 == "uv" {print $2}' /tmp/.tool-versions)" \
        python -m pip --use-deprecated=legacy-certs install --no-cache-dir --require-hashes \
        -r /tmp/python-bootstrap.lock -r /tmp/uv-bootstrap.lock \
    && rm -f /usr/local/bin/uv /usr/local/bin/uvx \
    && install -m 0755 /opt/hermes/.venv/bin/uv /usr/local/bin/uv \
    && install -m 0755 /opt/hermes/.venv/bin/uvx /usr/local/bin/uvx \
    && python -c 'import grpc, pathlib; roots = pathlib.Path(grpc.__file__).parent / "_cython/_credentials/roots.pem"; roots.unlink(); roots.symlink_to("/etc/ssl/certs/ca-certificates.crt")' \
    && python -c 'import ensurepip, pathlib, shutil; shutil.rmtree(pathlib.Path(ensurepip.__file__).parent)'

# Embedded Node roots in the fixed upstream base are superseded by system roots.
ENV NODE_OPTIONS=--use-openssl-ca \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/etc/ssl/certs/ca-certificates.crt

ARG HERMES_UID=1100
ARG HERMES_GID=1100

# Bake the NAS identity into the immutable layer. The upstream image otherwise
# edits /etc/passwd and /etc/group at every boot, which is incompatible with a
# read-only root filesystem.
RUN groupmod -o --gid "${HERMES_GID}" hermes \
    && usermod -o --uid "${HERMES_UID}" --gid "${HERMES_GID}" hermes \
    && chown "${HERMES_UID}:${HERMES_GID}" /opt/data

COPY deploy/compose/hermes-agent/entrypoint.sh /usr/local/bin/vonk-hermes-entrypoint
RUN chmod 0755 /usr/local/bin/vonk-hermes-entrypoint

ENTRYPOINT ["/usr/local/bin/vonk-hermes-entrypoint"]
CMD ["gateway", "run"]

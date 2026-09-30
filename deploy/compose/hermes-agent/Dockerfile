ARG HERMES_IMAGE=nousresearch/hermes-agent:v2026.9.24@sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7
FROM ${HERMES_IMAGE} AS managed

ARG HERMES_UID=1100
ARG HERMES_GID=1100

# Bake the NAS identity into the immutable layer. The upstream image otherwise
# edits /etc/passwd and /etc/group at every boot, which is incompatible with a
# read-only root filesystem.
RUN groupmod -o --gid "${HERMES_GID}" hermes \
    && usermod -o --uid "${HERMES_UID}" --gid "${HERMES_GID}" hermes \
    && chown "${HERMES_UID}:${HERMES_GID}" /opt/data

COPY entrypoint.sh /usr/local/bin/vonk-hermes-entrypoint
RUN chmod 0755 /usr/local/bin/vonk-hermes-entrypoint

ENTRYPOINT ["/usr/local/bin/vonk-hermes-entrypoint"]
CMD ["gateway", "run"]

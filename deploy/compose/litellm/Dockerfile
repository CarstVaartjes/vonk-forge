FROM ghcr.io/berriai/litellm:v1.83.14-stable.patch.3@sha256:f12d528d4a05add56cb09e54c5126088f2edc6bdf3a2f943bcd3a32b08769da2 AS upstream

FROM ghcr.io/berriai/litellm:v1.83.14-stable.patch.3@sha256:f12d528d4a05add56cb09e54c5126088f2edc6bdf3a2f943bcd3a32b08769da2 AS runtime

USER 0:0

COPY --from=upstream --chmod=0555 \
    /root/.cache/prisma-python/binaries/5.4.2/ac9d7041ed77bcc8a8dbd2ab6616b39013829574/node_modules/@prisma/engines/query-engine-* \
    /opt/vonk-litellm/prisma/query-engine

USER 10002:10001

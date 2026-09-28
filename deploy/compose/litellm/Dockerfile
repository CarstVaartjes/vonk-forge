FROM ghcr.io/berriai/litellm:v1.103.0 AS upstream

# Prisma keeps one query engine per platform under the image's own cache.
# Stage the native engine so the runtime layer can copy it without a glob.
USER 0:0
RUN set -eu; \
    case "$(uname -m)" in \
        x86_64) platform=debian-openssl-3.0.x ;; \
        aarch64) platform=linux-arm64-openssl-3.0.x ;; \
        *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    install -D -m 0555 \
        "/opt/prisma/.cache/prisma/master/ac9d7041ed77bcc8a8dbd2ab6616b39013829574/${platform}/query-engine" \
        /vonk-prisma/query-engine

FROM ghcr.io/berriai/litellm:v1.103.0 AS runtime

USER 0:0

COPY --from=upstream --chmod=0555 /vonk-prisma/query-engine /opt/vonk-litellm/prisma/query-engine

COPY --chmod=0444 agent_protocol/src/vonk_agent_protocol/route_activation.py /opt/vonk-litellm/route_activation.py

USER 10002:10001

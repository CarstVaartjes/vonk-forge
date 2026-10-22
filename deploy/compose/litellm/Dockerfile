ARG PYTHON_IMAGE=python:3.14-slim-trixie@sha256:0514bc76712d64f8bb48adcb95b1ab0d88c69fee95d62fe4c18ee948bee9b1e1
FROM --platform=$BUILDPLATFORM ${PYTHON_IMAGE} AS ca-bootstrap

FROM ghcr.io/berriai/litellm:v1.103.0@sha256:bd089afdcd35b894b14a93f9743cdc8b591f82da1a38dd43a010a7b0c9de5fd7 AS upstream

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

FROM ghcr.io/berriai/litellm:v1.103.0@sha256:bd089afdcd35b894b14a93f9743cdc8b591f82da1a38dd43a010a7b0c9de5fd7 AS runtime

USER 0:0

# Keep the reviewed LiteLLM version; refresh its system and Python trust stores.
# Bootstrap against the reviewed fresh CA store before any network operation.
# Wolfi still ships the compromised root; the final public store is certifi.
COPY --from=ca-bootstrap /etc/ssl/certs/ca-certificates.crt /tmp/bootstrap-ca.crt
COPY deploy/compose/python-bootstrap.lock /tmp/python-bootstrap.lock
# Bootstrap pip against only the refreshed OS bundle; its old native-trust
# mode would otherwise add the vendored roots back to that store.
RUN cp /tmp/bootstrap-ca.crt /etc/ssl/certs/ca-certificates.crt \
    && apk upgrade --no-cache ca-certificates-bundle \
    && python -m ensurepip \
    && PIP_CERT=/tmp/bootstrap-ca.crt python -m pip --use-deprecated=legacy-certs install --no-cache-dir --require-hashes \
        -r /tmp/python-bootstrap.lock \
    && python -c 'import certifi, shutil; shutil.copyfile(certifi.where(), "/etc/ssl/certs/ca-certificates.crt")' \
    && rm -f /tmp/bootstrap-ca.crt \
    && python -c 'import grpc, pathlib; roots = pathlib.Path(grpc.__file__).parent / "_cython/_credentials/roots.pem"; roots.unlink(); roots.symlink_to("/etc/ssl/certs/ca-certificates.crt")' \
    && python -c 'import ensurepip, pathlib, shutil; shutil.rmtree(pathlib.Path(ensurepip.__file__).parent)'

# Embedded Node roots in the fixed upstream base are superseded by system roots.
ENV NODE_OPTIONS=--use-openssl-ca \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/etc/ssl/certs/ca-certificates.crt

COPY --from=upstream --chmod=0555 /vonk-prisma/query-engine /opt/vonk-litellm/prisma/query-engine

COPY --chmod=0444 deploy/compose/litellm/patch-vllm-passthrough.py /opt/vonk-litellm/patch-vllm-passthrough.py
RUN python /opt/vonk-litellm/patch-vllm-passthrough.py

COPY --chmod=0444 agent_protocol/src/vonk_agent_protocol/route_activation.py /opt/vonk-litellm/route_activation.py
COPY --chmod=0444 agent_protocol/src/vonk_agent_protocol/route_activation_words.py /opt/vonk-litellm/route_activation_words.py

USER 10002:10001

# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:235600a8101ab264e117b1768e925532262668dc9b581ef1dd7d96ced463b8e7
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
ARG VONK_DEV_SOURCE_COMMIT=0000000000000000000000000000000000000000
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
COPY control/src ./src
COPY schemas/global/catalog-entity-v1.schema.json /schemas/global/catalog-entity-v1.schema.json
COPY schemas/global/harness-evidence-v1.schema.json /schemas/global/harness-evidence-v1.schema.json
COPY schemas/global/recipe-v1.schema.json /schemas/global/recipe-v1.schema.json
COPY schemas/global/test-report-v1.schema.json /schemas/global/test-report-v1.schema.json
COPY config/execution-harnesses /config/execution-harnesses
COPY --from=web /web/dist ./src/vonk_control/web
COPY inventory/wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl /wheels/
RUN python -m pip install --no-cache-dir --prefix=/install \
    /wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl /vonk-cluster-profiles .

FROM ${PYTHON_IMAGE} AS runtime-root
RUN groupadd --gid 10001 control \
    && useradd --uid 10001 --gid 10001 --no-create-home control
COPY --from=build /install /usr/local
WORKDIR /srv/vonk-control
COPY control/alembic.ini /srv/vonk-control/alembic.ini
COPY control/migrations /srv/vonk-control/migrations
RUN chmod -R a+rX /srv/vonk-control/migrations

FROM runtime-root AS worker-root
ARG VONK_DEV_SOURCE_COMMIT
RUN python - "$VONK_DEV_SOURCE_COMMIT" "worker" <<'PY'
import os
import sys
import tempfile

from vonk_control.dev_cohort import (
    DEVELOPMENT_IMAGE_IDENTITY_PATH,
    build_identity,
)

identity = build_identity(role=sys.argv[2], source_commit=sys.argv[1])
target = DEVELOPMENT_IMAGE_IDENTITY_PATH
target.parent.mkdir(parents=True, exist_ok=True)
descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{target.name}.", dir=target.parent
)
try:
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(identity.to_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary_name, 0o444)
    os.replace(temporary_name, target)
except BaseException:
    try:
        os.unlink(temporary_name)
    except FileNotFoundError:
        pass
    raise
PY

FROM runtime-root AS api-root
ARG VONK_DEV_SOURCE_COMMIT
RUN install -d -o 0 -g 0 -m 0700 /run/secrets
RUN python - "$VONK_DEV_SOURCE_COMMIT" "api" <<'PY'
import os
import sys
import tempfile

from vonk_control.dev_cohort import (
    DEVELOPMENT_IMAGE_IDENTITY_PATH,
    build_identity,
)

identity = build_identity(role=sys.argv[2], source_commit=sys.argv[1])
target = DEVELOPMENT_IMAGE_IDENTITY_PATH
target.parent.mkdir(parents=True, exist_ok=True)
descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{target.name}.", dir=target.parent
)
try:
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(identity.to_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary_name, 0o444)
    os.replace(temporary_name, target)
except BaseException:
    try:
        os.unlink(temporary_name)
    except FileNotFoundError:
        pass
    raise
PY

FROM scratch AS worker
COPY --from=worker-root / /
WORKDIR /srv/vonk-control
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001

FROM scratch AS api
COPY --from=api-root / /
WORKDIR /srv/vonk-control
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 0:0
ENTRYPOINT ["python", "-m", "vonk_control.api_preexec"]
CMD ["python", "-m", "vonk_control.api"]

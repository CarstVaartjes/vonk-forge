#!/bin/sh
set -eu

source=${LITELLM_DATABASE_PASSWORD_FILE:-/run/secrets/litellm-database-password}
runtime_directory=/run/vonk-postgres-secrets
target=$runtime_directory/litellm-database-password

# Standalone Compose implements secrets as read-only bind mounts. Their host
# UID is not portable to the image's postgres UID, so stage the one secret
# needed by initdb before the official entrypoint drops privileges.
install -d -m 0700 -o postgres -g postgres "$runtime_directory"
install -m 0400 -o postgres -g postgres "$source" "$target"
export LITELLM_DATABASE_PASSWORD_FILE=$target

exec /usr/local/bin/docker-entrypoint.sh "$@"

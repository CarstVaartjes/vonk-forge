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

sentinel=${PGDATA:-/var/lib/postgresql/data}/.vonk-database-initialized
if [ -f "${PGDATA:-/var/lib/postgresql/data}/PG_VERSION" ]; then
  existing_cluster=1
else
  existing_cluster=0
  export VONK_POSTGRES_INIT_SENTINEL=$sentinel
fi

/usr/local/bin/docker-entrypoint.sh "$@" &
postgres_pid=$!

forward_signal() {
  kill -TERM "$postgres_pid" 2>/dev/null || true
}
trap forward_signal TERM INT

ready=0
attempt=0
while [ "$attempt" -lt 240 ]; do
  if ! kill -0 "$postgres_pid" 2>/dev/null; then
    wait "$postgres_pid"
    exit $?
  fi
  if [ "$existing_cluster" -eq 1 ] || [ -f "$sentinel" ]; then
    if pg_isready --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" >/dev/null 2>&1; then
      ready=1
      break
    fi
  fi
  attempt=$((attempt + 1))
  sleep 1
done

if [ "$ready" -ne 1 ]; then
  printf '%s\n' 'PostgreSQL did not become ready for database reconciliation' >&2
  kill -TERM "$postgres_pid" 2>/dev/null || true
  wait "$postgres_pid" || true
  exit 1
fi

if ! /docker-entrypoint-initdb.d/10-vonk-forge-databases.sh; then
  printf '%s\n' 'PostgreSQL database reconciliation failed' >&2
  kill -TERM "$postgres_pid" 2>/dev/null || true
  wait "$postgres_pid" || true
  exit 1
fi

wait "$postgres_pid"

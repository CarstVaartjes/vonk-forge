#!/bin/sh
set -eu

password_file=${LITELLM_DATABASE_PASSWORD_FILE:-/run/secrets/litellm-database-password}
password=$(cat "$password_file")

case "$password" in
  *[!0-9a-f]*|'')
    printf '%s\n' 'LiteLLM database password is invalid' >&2
    exit 1
    ;;
esac

if [ "${#password}" -ne 64 ]; then
  printf '%s\n' 'LiteLLM database password is invalid' >&2
  exit 1
fi

psql \
  --set=ON_ERROR_STOP=1 \
  --set="litellm_password=$password" \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE litellm LOGIN PASSWORD %L', :'litellm_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'litellm')\gexec
ALTER ROLE litellm LOGIN PASSWORD :'litellm_password';
SELECT 'CREATE DATABASE litellm OWNER litellm'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'litellm')\gexec
ALTER DATABASE litellm OWNER TO litellm;
SQL

if [ -n "${VONK_POSTGRES_INIT_SENTINEL:-}" ]; then
  : >"$VONK_POSTGRES_INIT_SENTINEL"
fi

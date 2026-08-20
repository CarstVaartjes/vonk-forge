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
CREATE ROLE litellm LOGIN PASSWORD :'litellm_password';
CREATE DATABASE litellm OWNER litellm;
SQL

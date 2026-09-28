#!/bin/sh
set -eu

read_secret() {
    variable=$1
    source=$2
    if [ ! -r "$source" ]; then
        printf 'LiteLLM required secret file is unreadable\n' >&2
        exit 2
    fi
    value=$(cat -- "$source")
    if [ -z "$value" ]; then
        printf 'LiteLLM required secret file is empty\n' >&2
        exit 2
    fi
    export "$variable=$value"
}

read_secret LITELLM_MASTER_KEY "${LITELLM_MASTER_KEY_FILE:-/run/secrets/litellm-master-key}"
upstream_key_file=${LITELLM_UPSTREAM_KEY_FILE:-/run/secrets/litellm-upstream-key}
if [ -e "$upstream_key_file" ]; then
    if [ ! -r "$upstream_key_file" ]; then
        printf 'LiteLLM optional upstream key file is unreadable\n' >&2
        exit 2
    fi
    LITELLM_UPSTREAM_KEY=$(cat -- "$upstream_key_file")
else
    LITELLM_UPSTREAM_KEY=
fi
export LITELLM_UPSTREAM_KEY
read_secret LITELLM_DATABASE_URL "${LITELLM_DATABASE_URL_FILE:-/run/secrets/litellm-database-url}"
exec python /run/vonk-runtime-assets/litellm/config_supervisor.py

#!/bin/sh
set -eu

for source in \
    /run/secrets/litellm-master-key \
    /run/secrets/litellm-upstream-key \
    /run/secrets/litellm-database-url
do
    if [ -L "$source" ] || [ ! -f "$source" ] || [ ! -r "$source" ] || [ ! -s "$source" ]; then
        printf 'LiteLLM required secret file is unavailable\n' >&2
        exit 2
    fi
done
unset LITELLM_MASTER_KEY LITELLM_UPSTREAM_KEY LITELLM_DATABASE_URL
exec python /app/config-supervisor.py

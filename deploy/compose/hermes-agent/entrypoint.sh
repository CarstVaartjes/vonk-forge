#!/bin/sh
set -eu

test_root=${HERMES_ENTRYPOINT_TEST_ROOT:-}
secret_path="${test_root}/run/secrets/hermes-api-key"
litellm_key_path="${test_root}/run/secrets/hermes-litellm-key"

fail() {
    printf '%s\n' "ERROR: Hermes API key file is invalid" >&2
    exit 1
}

fail_litellm() {
    printf '%s\n' "ERROR: Hermes LiteLLM client key file is invalid" >&2
    exit 1
}

[ ! -L "${secret_path}" ] || fail
[ -f "${secret_path}" ] || fail

secret_size=$(wc -c <"${secret_path}") || fail
[ "${secret_size}" -le 4096 ] || fail

line_count=$(awk 'END { print NR }' "${secret_path}") || fail
[ "${line_count}" -eq 1 ] || fail

API_SERVER_KEY=$(sed 's/\r$//' "${secret_path}") || fail
[ "${#API_SERVER_KEY}" -ge 32 ] || fail
case "${API_SERVER_KEY}" in
    *[!A-Za-z0-9_.~-]*) fail ;;
esac
export API_SERVER_KEY
export HERMES_DASHBOARD_BASIC_AUTH_PASSWORD="${API_SERVER_KEY}"
export HERMES_DASHBOARD_BASIC_AUTH_SECRET="${API_SERVER_KEY}"

[ ! -L "${litellm_key_path}" ] || fail_litellm
[ -f "${litellm_key_path}" ] || fail_litellm

litellm_key_size=$(wc -c <"${litellm_key_path}") || fail_litellm
[ "${litellm_key_size}" -le 4096 ] || fail_litellm

litellm_key_lines=$(awk 'END { print NR }' "${litellm_key_path}") || fail_litellm
[ "${litellm_key_lines}" -eq 1 ] || fail_litellm

OPENAI_API_KEY=$(sed 's/\r$//' "${litellm_key_path}") || fail_litellm
[ "${#OPENAI_API_KEY}" -ge 35 ] || fail_litellm
case "${OPENAI_API_KEY}" in
    sk-[A-Za-z0-9_.~-]*) ;;
    *) fail_litellm ;;
esac
case "${OPENAI_API_KEY}" in
    *[!A-Za-z0-9_.~-]*) fail_litellm ;;
esac
export OPENAI_API_KEY

if [ "${HERMES_ENTRYPOINT_TEST_ONLY:-0}" = "1" ]; then
    exit 0
fi

exec /init /opt/hermes/docker/main-wrapper.sh "$@"

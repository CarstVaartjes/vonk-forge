#!/bin/sh
set -eu

socket=${TS_SOCKET_PATH:-/var/run/tailscale/tailscaled.sock}
remaining=120
reconcile_interval=${TS_RECONCILE_INTERVAL_SECONDS:-30}
hermes_api_host=${HERMES_API_HOST:-hermes-agent}
hermes_api_port=${HERMES_API_PORT:-8642}
hermes_dashboard_host=${HERMES_DASHBOARD_HOST:-hermes-agent}
hermes_dashboard_port=${HERMES_DASHBOARD_PORT:-9119}
hermes_api_key_path=${HERMES_API_KEY_PATH:-/run/secrets/hermes-api-key}
default_map_services_first='{"services":{"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}},"version":"0.0.1"}'
default_map_version_first='{"version":"0.0.1","services":{"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}}}'
hermes_map_services_first='{"services":{"svc:hermes-api":{"endpoints":{"tcp:443":"http://hermes-agent:8642"}},"svc:hermes-dashboard":{"endpoints":{"tcp:443":"http://hermes-agent:9119"}},"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}},"version":"0.0.1"}'
hermes_map_version_first='{"version":"0.0.1","services":{"svc:hermes-api":{"endpoints":{"tcp:443":"http://hermes-agent:8642"}},"svc:hermes-dashboard":{"endpoints":{"tcp:443":"http://hermes-agent:9119"}},"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}}}'

scratch_root=${TMPDIR:-/tmp}
runtime_dir=$(mktemp -d "${scratch_root%/}/vonk-tailscale.XXXXXX")
cleanup() {
    case "${runtime_dir:-}" in
        "${scratch_root%/}"/vonk-tailscale.*)
            rm -rf "${runtime_dir}"
            ;;
    esac
}
trap cleanup 0
trap 'exit 0' 1 2 15

ts() {
    tailscale --socket="${socket}" "$@"
}

http_probe() {
    host=$1
    port=$2
    path=$3
    authorization=$4
    response_path=$5

    {
        printf 'GET %s HTTP/1.1\r\n' "${path}"
        printf 'Host: %s:%s\r\n' "${host}" "${port}"
        if [ -n "${authorization}" ]; then
            printf 'Authorization: Bearer %s\r\n' "${authorization}"
        fi
        printf 'Connection: close\r\n\r\n'
    } | nc -w 3 "${host}" "${port}" >"${response_path}" 2>/dev/null \
        && grep -Eq '^HTTP/1\.[01] 2[0-9][0-9][[:space:]]' "${response_path}"
}

hermes_is_available() {
    [ -r "${hermes_api_key_path}" ] || return 1
    hermes_api_key=$(tr -d '\r\n' <"${hermes_api_key_path}")
    [ -n "${hermes_api_key}" ] || return 1
    http_probe \
        "${hermes_api_host}" \
        "${hermes_api_port}" \
        /health \
        "${hermes_api_key}" \
        "${runtime_dir}/hermes-api-response" \
        && http_probe \
            "${hermes_dashboard_host}" \
            "${hermes_dashboard_port}" \
            / \
            '' \
            "${runtime_dir}/hermes-dashboard-response"
}

serve_is_exact() {
    include_hermes=$1
    ts serve status --json >"${runtime_dir}/tailscale-serve-status.json"
    ts serve get-config --all >"${runtime_dir}/tailscale-serve-config.json"
    tr -d '[:space:]' \
        <"${runtime_dir}/tailscale-serve-status.json" \
        >"${runtime_dir}/tailscale-serve-status.compact"
    tr -d '[:space:]' \
        <"${runtime_dir}/tailscale-serve-config.json" \
        >"${runtime_dir}/tailscale-serve-config.compact"

    actual_service_map=$(cat "${runtime_dir}/tailscale-serve-config.compact")
    grep -Fq '"svc:vonk-forge":{"TCP":{"443":{"HTTPS":true}}' \
        "${runtime_dir}/tailscale-serve-status.compact" \
        && ! grep -Fq '"443":{"HTTP":true}' "${runtime_dir}/tailscale-serve-status.compact" \
        && ! grep -Fq '"TCPForward"' "${runtime_dir}/tailscale-serve-status.compact" \
        && if [ "${include_hermes}" = "1" ]; then
            grep -Fq '"svc:hermes-api":{"TCP":{"443":{"HTTPS":true}}' \
                "${runtime_dir}/tailscale-serve-status.compact" \
                && grep -Fq '"svc:hermes-dashboard":{"TCP":{"443":{"HTTPS":true}}' \
                    "${runtime_dir}/tailscale-serve-status.compact" \
                && { [ "${actual_service_map}" = "${hermes_map_services_first}" ] \
                    || [ "${actual_service_map}" = "${hermes_map_version_first}" ]; }
        else
            ! grep -Fq 'svc:hermes-' "${runtime_dir}/tailscale-serve-status.compact" \
                && { [ "${actual_service_map}" = "${default_map_services_first}" ] \
                    || [ "${actual_service_map}" = "${default_map_version_first}" ]; }
        fi
}

configure_services() {
    include_hermes=$1
    # Configuration-file import currently infers the listener protocol from the
    # HTTP upstream and can create plaintext HTTP on port 443. Express the
    # listener protocol explicitly through the CLI instead.
    # Reset the complete map so undeclared services or endpoints cannot survive
    # reconciliation from an earlier gateway configuration.
    ts serve reset
    ts serve --service=svc:vonk-forge --https=443 http://caddy:8080
    ts serve advertise svc:vonk-forge
    if [ "${include_hermes}" = "1" ]; then
        ts serve --service=svc:hermes-api --https=443 http://hermes-agent:8642
        ts serve --service=svc:hermes-dashboard --https=443 http://hermes-agent:9119
        ts serve advertise svc:hermes-api
        ts serve advertise svc:hermes-dashboard
    fi
}

wait_for_exact_services() {
    include_hermes=$1
    activation_remaining=120
    while [ "${activation_remaining}" -gt 0 ]; do
        if serve_is_exact "${include_hermes}"; then
            return 0
        fi
        sleep 2
        activation_remaining=$((activation_remaining - 2))
    done
    return 1
}

capability_is_available() {
    ts status --json >"${runtime_dir}/tailscale-status.json" \
        && { grep -Fq '"service-host"' "${runtime_dir}/tailscale-status.json" \
            || grep -Fq '"services/vonk-forge"' "${runtime_dir}/tailscale-status.json"; }
}

include_hermes=0
hermes_is_available && include_hermes=1

if [ "${TS_HEALTHCHECK_ONLY:-0}" = "1" ]; then
    [ -S "${socket}" ] && capability_is_available \
        && serve_is_exact "${include_hermes}"
    exit
fi

while [ "${remaining}" -gt 0 ]; do
    if [ -S "${socket}" ] && capability_is_available; then
        break
    fi
    sleep 2
    remaining=$((remaining - 2))
done

if [ "${remaining}" -le 0 ]; then
    echo "ERROR: Tailscale did not become ready within 120 seconds." >&2
    exit 1
fi

if ! serve_is_exact "${include_hermes}"; then
    configure_services "${include_hermes}"
fi
if ! wait_for_exact_services "${include_hermes}"; then
    echo "ERROR: Tailscale Services do not have the exact HTTPS listeners." >&2
    exit 1
fi

if [ "${TS_CONFIGURE_ONCE:-0}" = "1" ]; then
    exit 0
fi

# Persist as a small reconciler. If gateway state is restored or replaced while
# this Compose project stays up, a missing or downgraded listener is repaired
# without an operator having to recreate this container.
while :; do
    sleep "${reconcile_interval}"
    desired_hermes=0
    hermes_is_available && desired_hermes=1
    if ! serve_is_exact "${desired_hermes}"; then
        configure_services "${desired_hermes}"
        wait_for_exact_services "${desired_hermes}" || exit 1
    fi
done

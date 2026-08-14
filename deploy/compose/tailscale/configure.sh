#!/bin/sh
set -eu

socket=${TS_SOCKET_PATH:-/var/run/tailscale/tailscaled.sock}
remaining=120
expected_service_map_services_first='{"services":{"svc:hermes-api":{"endpoints":{"tcp:443":"http://hermes-agent:8642"}},"svc:hermes-dashboard":{"endpoints":{"tcp:443":"http://hermes-agent:9119"}},"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}},"version":"0.0.1"}'
expected_service_map_version_first='{"version":"0.0.1","services":{"svc:hermes-api":{"endpoints":{"tcp:443":"http://hermes-agent:8642"}},"svc:hermes-dashboard":{"endpoints":{"tcp:443":"http://hermes-agent:9119"}},"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}}}'

ts() {
    tailscale --socket="${socket}" "$@"
}

while [ "${remaining}" -gt 0 ]; do
    if [ -S "${socket}" ] && ts status --json >/tmp/tailscale-status.json 2>/dev/null; then
        break
    fi
    sleep 2
    remaining=$((remaining - 2))
done

if [ "${remaining}" -le 0 ]; then
    echo "ERROR: Tailscale did not become ready within 120 seconds." >&2
    exit 1
fi

serve_is_exact() {
    ts serve status --json >/tmp/tailscale-serve-status.json
    ts serve get-config --all >/tmp/tailscale-serve-config.json
    tr -d '[:space:]' </tmp/tailscale-serve-status.json >/tmp/tailscale-serve-status.compact
    tr -d '[:space:]' </tmp/tailscale-serve-config.json >/tmp/tailscale-serve-config.compact

    actual_service_map=$(cat /tmp/tailscale-serve-config.compact)
    grep -Fq '"svc:vonk-forge":{"TCP":{"443":{"HTTPS":true}}' \
        /tmp/tailscale-serve-status.compact \
        && ! grep -Fq '"443":{"HTTP":true}' /tmp/tailscale-serve-status.compact \
        && ! grep -Fq '"TCPForward"' /tmp/tailscale-serve-status.compact \
        && grep -Fq '"svc:hermes-api":{"TCP":{"443":{"HTTPS":true}}' \
            /tmp/tailscale-serve-status.compact \
        && grep -Fq '"svc:hermes-dashboard":{"TCP":{"443":{"HTTPS":true}}' \
            /tmp/tailscale-serve-status.compact \
        && { [ "${actual_service_map}" = "${expected_service_map_services_first}" ] \
            || [ "${actual_service_map}" = "${expected_service_map_version_first}" ]; }
}

configure_services() {
    # Configuration-file import currently infers the listener protocol from the
    # HTTP upstream and can create plaintext HTTP on port 443. Express the
    # listener protocol explicitly through the CLI instead.
    # Reset the complete map so undeclared services or endpoints cannot survive
    # reconciliation from an earlier gateway configuration.
    ts serve reset
    ts serve --service=svc:vonk-forge --https=443 http://caddy:8080
    ts serve --service=svc:hermes-api --https=443 http://hermes-agent:8642
    ts serve --service=svc:hermes-dashboard --https=443 http://hermes-agent:9119
    ts serve advertise svc:vonk-forge
    ts serve advertise svc:hermes-api
    ts serve advertise svc:hermes-dashboard
}

wait_for_exact_services() {
    activation_remaining=120
    while [ "${activation_remaining}" -gt 0 ]; do
        if serve_is_exact; then
            return 0
        fi
        sleep 2
        activation_remaining=$((activation_remaining - 2))
    done
    return 1
}

if ! serve_is_exact; then
    configure_services
fi
if ! wait_for_exact_services; then
    echo "ERROR: Tailscale Services do not have the exact HTTPS listeners." >&2
    exit 1
fi

ts status --json >/tmp/tailscale-status.json
if grep -Fq '"service-host"' /tmp/tailscale-status.json; then
    :
elif grep -Fq '"services/vonk-forge"' /tmp/tailscale-status.json \
    && grep -Fq '"services/hermes-api"' /tmp/tailscale-status.json \
    && grep -Fq '"services/hermes-dashboard"' /tmp/tailscale-status.json; then
    :
else
    echo "ERROR: the gateway lacks the Tailscale service-host capability." >&2
    exit 1
fi

if [ "${TS_CONFIGURE_ONCE:-0}" = "1" ]; then
    exit 0
fi

# Persist as a small reconciler. If gateway state is restored or replaced while
# this Compose project stays up, a missing or downgraded listener is repaired
# without an operator having to recreate this container.
while :; do
    sleep 60
    if ! serve_is_exact; then
        configure_services
        wait_for_exact_services || exit 1
    fi
done

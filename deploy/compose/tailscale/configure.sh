#!/bin/sh
set -eu

socket=${TS_SOCKET_PATH:-/var/run/tailscale/tailscaled.sock}
remaining=120
reconcile_interval=${TS_RECONCILE_INTERVAL_SECONDS:-30}
default_map_services_first='{"services":{"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}},"version":"0.0.1"}'
default_map_version_first='{"version":"0.0.1","services":{"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}}}'
hermes_map_services_first='{"services":{"svc:hermes-api":{"endpoints":{"tcp:443":"http://hermes-agent:8642"}},"svc:hermes-dashboard":{"endpoints":{"tcp:443":"http://hermes-agent:9119"}},"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}},"version":"0.0.1"}'
hermes_map_version_first='{"version":"0.0.1","services":{"svc:hermes-api":{"endpoints":{"tcp:443":"http://hermes-agent:8642"}},"svc:hermes-dashboard":{"endpoints":{"tcp:443":"http://hermes-agent:9119"}},"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}}}'

ts() {
    tailscale --socket="${socket}" "$@"
}

hermes_is_available() {
    nc -z -w 3 hermes-agent 8642 >/dev/null 2>&1 \
        && nc -z -w 3 hermes-agent 9119 >/dev/null 2>&1
}

serve_is_exact() {
    include_hermes=$1
    ts serve status --json >/tmp/tailscale-serve-status.json
    ts serve get-config --all >/tmp/tailscale-serve-config.json
    tr -d '[:space:]' </tmp/tailscale-serve-status.json >/tmp/tailscale-serve-status.compact
    tr -d '[:space:]' </tmp/tailscale-serve-config.json >/tmp/tailscale-serve-config.compact

    actual_service_map=$(cat /tmp/tailscale-serve-config.compact)
    grep -Fq '"svc:vonk-forge":{"TCP":{"443":{"HTTPS":true}}' \
        /tmp/tailscale-serve-status.compact \
        && ! grep -Fq '"443":{"HTTP":true}' /tmp/tailscale-serve-status.compact \
        && ! grep -Fq '"TCPForward"' /tmp/tailscale-serve-status.compact \
        && if [ "${include_hermes}" = "1" ]; then
            grep -Fq '"svc:hermes-api":{"TCP":{"443":{"HTTPS":true}}' \
                /tmp/tailscale-serve-status.compact \
                && grep -Fq '"svc:hermes-dashboard":{"TCP":{"443":{"HTTPS":true}}' \
                    /tmp/tailscale-serve-status.compact \
                && { [ "${actual_service_map}" = "${hermes_map_services_first}" ] \
                    || [ "${actual_service_map}" = "${hermes_map_version_first}" ]; }
        else
            ! grep -Fq 'svc:hermes-' /tmp/tailscale-serve-status.compact \
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
    ts status --json >/tmp/tailscale-status.json \
        && { grep -Fq '"service-host"' /tmp/tailscale-status.json \
            || grep -Fq '"services/vonk-forge"' /tmp/tailscale-status.json; }
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

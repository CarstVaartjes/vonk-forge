#!/bin/sh
set -eu

socket=${TS_SOCKET_PATH:-/var/run/tailscale/tailscaled.sock}
hostname_output=${TS_HOSTNAME_OUTPUT:-/run/vonk-tailnet/control-hostname}
remaining=120
expected_service_map='{"services":{"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}},"version":"0.0.1"}'

ts() {
    tailscale --socket="${socket}" "$@"
}

case "${hostname_output}" in
    /*/control-hostname) ;;
    *)
        printf 'ERROR: browser hostname output path is invalid.\n' >&2
        exit 1
        ;;
esac
rm -f "${hostname_output}"

while [ "${remaining}" -gt 0 ]; do
    if [ -S "${socket}" ] && ts status --json >/tmp/tailscale-status.json 2>/dev/null; then
        break
    fi
    sleep 2
    remaining=$((remaining - 2))
done

if [ "${remaining}" -le 0 ]; then
    printf 'ERROR: Tailscale did not become ready within 120 seconds.\n' >&2
    exit 1
fi

serve_is_exact() {
    ts serve status --json >/tmp/tailscale-serve-status.json
    ts serve get-config --all >/tmp/tailscale-serve-config.json
    tr -d '[:space:]' </tmp/tailscale-serve-status.json >/tmp/tailscale-serve-status.compact
    tr -d '[:space:]' </tmp/tailscale-serve-config.json >/tmp/tailscale-serve-config.compact

    grep -Fq '"svc:vonk-forge":{"TCP":{"443":{"HTTPS":true}}' \
        /tmp/tailscale-serve-status.compact \
        && ! grep -Fq '"443":{"HTTP":true}' /tmp/tailscale-serve-status.compact \
        && ! grep -Fq '"TCPForward"' /tmp/tailscale-serve-status.compact \
        && [ "$(cat /tmp/tailscale-serve-config.compact)" = "${expected_service_map}" ]
}

configure_service() {
    ts serve reset
    ts serve --service=svc:vonk-forge --https=443 http://caddy:8080
    ts serve advertise svc:vonk-forge
}

verify_capability_and_suffix() {
    ts status --json >/tmp/tailscale-status.json
    if ! grep -Fq 'service-host' /tmp/tailscale-status.json; then
        printf 'ERROR: the gateway lacks the required Service hosting capability.\n' >&2
        return 1
    fi
    suffix=$(sed -n 's/.*"MagicDNSSuffix"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /tmp/tailscale-status.json | head -n 1 | tr '[:upper:]' '[:lower:]')
    case "${suffix}" in
        "" | .* | *..* | *. | *[!a-z0-9.-]* | *-.* | *.-*)
            printf 'ERROR: Tailscale returned an invalid browser hostname suffix.\n' >&2
            return 1
            ;;
        *.ts.net) ;;
        *)
            printf 'ERROR: Tailscale returned an invalid browser hostname suffix.\n' >&2
            return 1
            ;;
    esac
    old_ifs=$IFS
    IFS=.
    set -- ${suffix}
    IFS=$old_ifs
    for label in "$@"; do
        case "${label}" in
            "" | -* | *-)
                printf 'ERROR: Tailscale returned an invalid browser hostname suffix.\n' >&2
                return 1
                ;;
        esac
    done
    service_hostname=vonk-forge.${suffix}
}

publish_hostname() {
    output_directory=${hostname_output%/*}
    if [ -L "${output_directory}" ] || [ ! -d "${output_directory}" ] || [ ! -w "${output_directory}" ]; then
        printf 'ERROR: browser hostname output directory is unavailable.\n' >&2
        return 1
    fi
    temporary=${hostname_output}.new.$$
    trap 'rm -f "${temporary}"' EXIT HUP INT TERM
    umask 022
    printf '%s\n' "${service_hostname}" >"${temporary}"
    chmod 0444 "${temporary}"
    mv -f "${temporary}" "${hostname_output}"
    trap - EXIT HUP INT TERM
    printf 'Vonk Forge browser URL: https://%s/\n' "${service_hostname}"
}

if ! serve_is_exact; then
    configure_service
fi
if ! serve_is_exact; then
    printf 'ERROR: the exact HTTPS Service map is not active.\n' >&2
    exit 1
fi
verify_capability_and_suffix
publish_hostname

if [ "${TS_CONFIGURE_ONCE:-0}" = "1" ]; then
    exit 0
fi

# Caddy's agent-only stage starts independently. Confirm that the browser edge
# has reloaded with the published canonical host before steady reconciliation.
remaining=120
while [ "${remaining}" -gt 0 ]; do
    if wget -q --spider -T 3 --header="Host: ${service_hostname}" http://caddy:8080/healthz; then
        break
    fi
    sleep 2
    remaining=$((remaining - 2))
done
if [ "${remaining}" -le 0 ]; then
    printf 'ERROR: Caddy did not become ready within 120 seconds.\n' >&2
    exit 1
fi

while :; do
    sleep 60
    if ! serve_is_exact; then
        configure_service
        serve_is_exact || exit 1
    fi
    verify_capability_and_suffix || exit 1
done

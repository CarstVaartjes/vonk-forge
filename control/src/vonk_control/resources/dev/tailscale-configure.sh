#!/bin/sh
set -eu

socket=${TS_SOCKET_PATH:-/var/run/tailscale/tailscaled.sock}
hostname_output=${TS_HOSTNAME_OUTPUT:-/run/vonk-tailnet/control-hostname}
authority_output=${hostname_output}.ready
generation_file=${TS_GENERATION_FILE:-/tmp/vonk-tailnet-generation}
reconcile_interval=60
remaining=120
expected_service_map='{"services":{"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}},"version":"0.0.1"}'

if [ "${TS_CONFIGURE_TEST_MODE:-0}" = "1" ]; then
    reconcile_interval=${TS_RECONCILE_INTERVAL:-1}
    case "${reconcile_interval}" in
        "" | *[!0-9]*)
            printf 'ERROR: test reconciliation interval is invalid.\n' >&2
            exit 1
            ;;
    esac
    if [ "${reconcile_interval}" -lt 1 ] || [ "${reconcile_interval}" -gt 5 ]; then
        printf 'ERROR: test reconciliation interval is invalid.\n' >&2
        exit 1
    fi
elif [ -n "${TS_RECONCILE_INTERVAL:-}${TS_TEST_GENERATION:-}" ]; then
    printf 'ERROR: test-only configurator controls require test mode.\n' >&2
    exit 1
fi

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

valid_generation() {
    printf '%s\n' "$1" | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
}

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

atomic_output() {
    destination=$1
    value=$2
    temporary=${destination}.new.$$
    umask 022
    printf '%s\n' "${value}" >"${temporary}"
    chmod 0444 "${temporary}"
    mv -f "${temporary}" "${destination}"
}

publish_hostname() {
    output_directory=${hostname_output%/*}
    if [ -L "${output_directory}" ] || [ ! -d "${output_directory}" ] || [ ! -w "${output_directory}" ]; then
        printf 'ERROR: browser hostname output directory is unavailable.\n' >&2
        return 1
    fi
    atomic_output "${hostname_output}" "${service_hostname}"
    # Publish authority last: readers can never authorize a hostname before the
    # matching operational output and this configurator generation are durable.
    atomic_output "${authority_output}" "${generation} ${service_hostname}"
    if [ "${published_hostname:-}" != "${service_hostname}" ]; then
        printf 'Vonk Forge browser URL: https://%s/\n' "${service_hostname}"
    fi
    published_hostname=${service_hostname}
}

fail_closed_outputs() {
    rm -f "${authority_output}" "${hostname_output}"
}

read_single_line() {
    path=$1
    [ ! -L "${path}" ] && [ -f "${path}" ] && [ -r "${path}" ] \
        && [ -s "${path}" ] && [ "$(wc -l <"${path}")" -eq 1 ] \
        && cat "${path}"
}

health() {
    generation=$(read_single_line "${generation_file}") || return 1
    valid_generation "${generation}" || return 1
    serve_is_exact || return 1
    verify_capability_and_suffix || return 1
    [ "$(read_single_line "${hostname_output}")" = "${service_hostname}" ] || return 1
    [ "$(read_single_line "${authority_output}")" = "${generation} ${service_hostname}" ] || return 1
    wget -q --spider -T 3 --header="Host: ${service_hostname}" http://caddy:8080/healthz
}

case "${1:-}" in
    health)
        [ "$#" -eq 1 ] || exit 1
        health
        exit $?
        ;;
    "") ;;
    *)
        printf 'ERROR: unsupported configurator mode.\n' >&2
        exit 1
        ;;
esac

output_directory=${hostname_output%/*}
if [ -L "${output_directory}" ] || [ ! -d "${output_directory}" ] || [ ! -w "${output_directory}" ]; then
    printf 'ERROR: browser hostname output directory is unavailable.\n' >&2
    exit 1
fi
fail_closed_outputs

if [ "${TS_CONFIGURE_TEST_MODE:-0}" = "1" ] && [ -n "${TS_TEST_GENERATION:-}" ]; then
    generation=${TS_TEST_GENERATION}
else
    generation=$(tr '[:upper:]' '[:lower:]' </proc/sys/kernel/random/uuid)
fi
if ! valid_generation "${generation}"; then
    printf 'ERROR: configurator generation is invalid.\n' >&2
    exit 1
fi
atomic_output "${generation_file}" "${generation}"
trap 'fail_closed_outputs; exit 1' HUP INT TERM

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

if ! serve_is_exact; then
    configure_service
fi
if ! serve_is_exact; then
    printf 'ERROR: the exact HTTPS Service map is not active.\n' >&2
    exit 1
fi
if ! verify_capability_and_suffix || ! publish_hostname; then
    fail_closed_outputs
    exit 1
fi

if [ "${TS_CONFIGURE_ONCE:-0}" = "1" ]; then
    trap - HUP INT TERM
    exit 0
fi

# Caddy starts agent-only and independently becomes healthy. Confirm that its
# watcher consumed this generation's live hostname before steady reconciliation.
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
    fail_closed_outputs
    exit 1
fi

while :; do
    sleep "${reconcile_interval}"
    if ! serve_is_exact; then
        configure_service
        if ! serve_is_exact; then
            fail_closed_outputs
            exit 1
        fi
    fi
    if ! verify_capability_and_suffix || ! publish_hostname; then
        fail_closed_outputs
        exit 1
    fi
done

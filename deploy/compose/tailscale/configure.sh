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
selected_profiles=${VONK_SELECTED_PROFILES:-}
control_service=${VONK_TAILSCALE_CONTROL_SERVICE:-svc:vonk-forge}
hermes_api_service=${VONK_TAILSCALE_HERMES_API_SERVICE:-svc:hermes-api}
hermes_dashboard_service=${VONK_TAILSCALE_HERMES_DASHBOARD_SERVICE:-svc:hermes-dashboard}

validate_service_name() {
    printf '%s\n' "$1" \
        | grep -Eq '^svc:[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'
}

validate_service_name "${control_service}" \
    && validate_service_name "${hermes_api_service}" \
    && validate_service_name "${hermes_dashboard_service}" \
    || { echo "ERROR: Tailscale Service names are invalid." >&2; exit 64; }
[ "${control_service}" != "${hermes_api_service}" ] \
    && [ "${control_service}" != "${hermes_dashboard_service}" ] \
    && [ "${hermes_api_service}" != "${hermes_dashboard_service}" ] \
    || { echo "ERROR: Tailscale Service names must be distinct." >&2; exit 64; }

default_map_services_first=$(printf \
    '{"services":{"%s":{"endpoints":{"tcp:443":"http://caddy:8080"}}},"version":"0.0.1"}' \
    "${control_service}")
default_map_version_first=$(printf \
    '{"version":"0.0.1","services":{"%s":{"endpoints":{"tcp:443":"http://caddy:8080"}}}}' \
    "${control_service}")
hermes_map_services_first=$(printf \
    '{"services":{"%s":{"endpoints":{"tcp:443":"http://hermes-agent:8642"}},"%s":{"endpoints":{"tcp:443":"http://hermes-agent:9119"}},"%s":{"endpoints":{"tcp:443":"http://caddy:8080"}}},"version":"0.0.1"}' \
    "${hermes_api_service}" "${hermes_dashboard_service}" "${control_service}")
hermes_map_version_first=$(printf \
    '{"version":"0.0.1","services":{"%s":{"endpoints":{"tcp:443":"http://hermes-agent:8642"}},"%s":{"endpoints":{"tcp:443":"http://hermes-agent:9119"}},"%s":{"endpoints":{"tcp:443":"http://caddy:8080"}}}}' \
    "${hermes_api_service}" "${hermes_dashboard_service}" "${control_service}")

scratch_root=${TMPDIR:-/tmp}
runtime_dir=$(mktemp -d "${scratch_root%/}/vonk-tailscale.XXXXXX")
empty_service_map=${runtime_dir}/empty-service-map.json
printf '%s\n' '{"version":"0.0.1","services":{}}' >"${empty_service_map}"
cleanup() {
    case "${runtime_dir:-}" in
        "${scratch_root%/}"/vonk-tailscale.*)
            rm -rf "${runtime_dir}"
            ;;
    esac
}
trap cleanup 0

ts() {
    tailscale --socket="${socket}" "$@"
}

http_probe() {
    host=$1
    port=$2
    path=$3
    authorization=$4
    response_path=$5
    accepted_statuses=$6

    {
        printf 'GET %s HTTP/1.1\r\n' "${path}"
        printf 'Host: %s:%s\r\n' "${host}" "${port}"
        if [ -n "${authorization}" ]; then
            printf 'Authorization: Bearer %s\r\n' "${authorization}"
        fi
        printf 'Connection: close\r\n\r\n'
        # BusyBox nc exits as soon as piped stdin reaches EOF, which can race
        # the peer's first response byte. Keep stdin open for one bounded
        # second so the final-read timeout can observe the HTTP status.
        sleep 1
    } | nc -w 3 "${host}" "${port}" >"${response_path}" 2>/dev/null \
        && grep -Eq "^HTTP/1\\.[01] ${accepted_statuses}[[:space:]]" "${response_path}"
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
        '2[0-9][0-9]' \
        && http_probe \
            "${hermes_dashboard_host}" \
            "${hermes_dashboard_port}" \
            / \
            '' \
            "${runtime_dir}/hermes-dashboard-response" \
            '[23][0-9][0-9]'
}

withdraw_hermes_services() {
    # Tailscale stores Serve config and AdvertiseServices
    # independently. Drain before reading the map so stale advertisements are
    # removed even when there is no corresponding Serve config to inspect.
    # Clear then removes any remaining handler. Both commands are idempotent;
    # suppress their expected informational output while preserving failures.
    ts serve drain "${hermes_api_service}" >/dev/null 2>&1
    ts serve clear "${hermes_api_service}" >/dev/null 2>&1
    ts serve drain "${hermes_dashboard_service}" >/dev/null 2>&1
    ts serve clear "${hermes_dashboard_service}" >/dev/null 2>&1
}

withdraw_all_services() {
    # A stopped gateway must not remain registered as a candidate host for the
    # fixed Service VIPs. Drain first, then atomically clear the full map and
    # AdvertiseServices preference before tailscaled itself is stopped.
    ts serve drain "${control_service}" >/dev/null 2>&1 || true
    ts serve drain "${hermes_api_service}" >/dev/null 2>&1 || true
    ts serve drain "${hermes_dashboard_service}" >/dev/null 2>&1 || true
    ts serve set-config --all "${empty_service_map}" >/dev/null 2>&1 || true
}

shutdown_services() {
    trap - 1 2 15
    if [ -S "${socket}" ]; then
        withdraw_all_services
    fi
    exit 0
}

serve_is_exact() {
    include_hermes=$1
    if [ "${include_hermes}" = "0" ]; then
        withdraw_hermes_services
    fi
    ts serve status --json >"${runtime_dir}/tailscale-serve-status.json"
    ts serve get-config --all >"${runtime_dir}/tailscale-serve-config.json"
    tr -d '[:space:]' \
        <"${runtime_dir}/tailscale-serve-status.json" \
        >"${runtime_dir}/tailscale-serve-status.compact"
    tr -d '[:space:]' \
        <"${runtime_dir}/tailscale-serve-config.json" \
        >"${runtime_dir}/tailscale-serve-config.compact"

    actual_service_map=$(cat "${runtime_dir}/tailscale-serve-config.compact")
    grep -Fq "\"${control_service}\":{\"TCP\":{\"443\":{\"HTTPS\":true}}" \
        "${runtime_dir}/tailscale-serve-status.compact" \
        && ! grep -Fq '"443":{"HTTP":true}' "${runtime_dir}/tailscale-serve-status.compact" \
        && ! grep -Fq '"TCPForward"' "${runtime_dir}/tailscale-serve-status.compact" \
        && if [ "${include_hermes}" = "1" ]; then
            grep -Fq "\"${hermes_api_service}\":{\"TCP\":{\"443\":{\"HTTPS\":true}}" \
                "${runtime_dir}/tailscale-serve-status.compact" \
                && grep -Fq "\"${hermes_dashboard_service}\":{\"TCP\":{\"443\":{\"HTTPS\":true}}" \
                    "${runtime_dir}/tailscale-serve-status.compact" \
                && { [ "${actual_service_map}" = "${hermes_map_services_first}" ] \
                    || [ "${actual_service_map}" = "${hermes_map_version_first}" ]; }
        else
            ! grep -Fq "\"${hermes_api_service}\":" "${runtime_dir}/tailscale-serve-status.compact" \
                && ! grep -Fq "\"${hermes_dashboard_service}\":" "${runtime_dir}/tailscale-serve-status.compact" \
                && { [ "${actual_service_map}" = "${default_map_services_first}" ] \
                    || [ "${actual_service_map}" = "${default_map_version_first}" ]; }
        fi
}

configure_services() {
    include_hermes=$1
    # Configuration-file import currently infers the listener protocol from the
    # HTTP upstream and can create plaintext HTTP on port 443. Express the
    # listener protocol explicitly through the CLI instead.
    # Applying an empty all-services file clears both the complete Serve map
    # and AdvertiseServices. Re-add endpoints through the CLI so port 443 is
    # explicitly HTTPS. Advertise each completed endpoint map explicitly: this
    # is idempotent and avoids depending on the configuration command's
    # asynchronous implicit advertisement reaching control.
    ts serve set-config --all "${empty_service_map}" >/dev/null
    ts serve --service="${control_service}" --https=443 http://caddy:8080 >/dev/null
    ts serve advertise "${control_service}" >/dev/null
    if [ "${include_hermes}" = "1" ]; then
        ts serve --service="${hermes_api_service}" --https=443 http://hermes-agent:8642 >/dev/null
        ts serve advertise "${hermes_api_service}" >/dev/null
        ts serve --service="${hermes_dashboard_service}" --https=443 http://hermes-agent:9119 >/dev/null
        ts serve advertise "${hermes_dashboard_service}" >/dev/null
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

extract_json_array() {
    key=$1
    source=$2
    destination=$3
    awk -v key="${key}" '
        BEGIN { marker = "\"" key "\":" }
        { document = document $0 }
        END {
            start = index(document, marker)
            if (start == 0) exit 1
            value = substr(document, start + length(marker))
            if (substr(value, 1, 1) != "[") exit 1
            depth = 0
            for (index_value = 1; index_value <= length(value); index_value++) {
                character = substr(value, index_value, 1)
                if (character == "[") depth++
                if (character == "]") {
                    depth--
                    if (depth == 0) {
                        print substr(value, 1, index_value)
                        exit 0
                    }
                }
            }
            exit 1
        }
    ' "${source}" >"${destination}"
}

service_has_mapped_addresses() {
    service_name=$1
    addresses=$(sed -n \
        "s/.*\"${service_name}\":\\[\\([^]]*\\)\\].*/\\1/p" \
        "${service_hosts}")
    [ -n "${addresses}" ] || return 1
}

service_host_is_approved() {
    include_hermes=$1
    service_has_mapped_addresses "${control_service}" || return 1
    if [ "${include_hermes}" = "0" ]; then
        return 0
    fi
    service_has_mapped_addresses "${hermes_api_service}" \
        && service_has_mapped_addresses "${hermes_dashboard_service}"
}

service_has_primary_routes() {
    service_name=$1
    service_has_mapped_addresses "${service_name}" || return 1
    previous_ifs=$IFS
    IFS=,
    set -- ${addresses}
    IFS=${previous_ifs}
    [ "$#" -gt 0 ] || return 1
    for quoted_address in "$@"; do
        case "${quoted_address}" in
            \"*\")
                address=${quoted_address#\"}
                address=${address%\"}
                ;;
            *) return 1 ;;
        esac
        case "${address}" in
            ''|*[!0-9a-fA-F:.]*) return 1 ;;
            *:*) prefix=128 ;;
            *) prefix=32 ;;
        esac
        grep -Fq "\"${address}/${prefix}\"" "${primary_routes}" \
            || return 1
    done
}

service_host_is_active() {
    include_hermes=$1
    ts status --json >"${runtime_dir}/tailscale-status.json" || return 1
    tr -d '[:space:]' \
        <"${runtime_dir}/tailscale-status.json" \
        >"${runtime_dir}/tailscale-status.compact"

    # services/* capabilities authorize this node as a client of a TailVIP;
    # they do not prove that control has approved this tagged node to host it.
    # service-host maps approved names to TailVIPs. PrimaryRoutes proves the
    # userspace host actually owns those routes; without it, clients resolve
    # the VIP but report "no matching peer" and HTTPS silently times out.
    service_hosts=${runtime_dir}/tailscale-service-hosts.compact
    primary_routes=${runtime_dir}/tailscale-primary-routes.compact
    extract_json_array service-host \
        "${runtime_dir}/tailscale-status.compact" "${service_hosts}" \
        || return 1
    extract_json_array PrimaryRoutes \
        "${runtime_dir}/tailscale-status.compact" "${primary_routes}" \
        || return 1
    service_has_primary_routes "${control_service}" \
        || return 1
    if [ "${include_hermes}" = "0" ]; then
        return 0
    fi
    service_has_primary_routes "${hermes_api_service}" \
        && service_has_primary_routes "${hermes_dashboard_service}"
}

include_hermes=0
hermes_is_available && include_hermes=1

if [ "${TS_HEALTHCHECK_ONLY:-0}" = "1" ]; then
    case ",${selected_profiles}," in
        *,hermes,*)
            if ! [ -S "${socket}" ]; then
                echo "ERROR: Tailscale configurator healthcheck: socket unavailable" >&2
                exit 1
            fi
            if ! service_host_is_active 1; then
                echo "ERROR: Tailscale configurator healthcheck: Service host routes are not active" >&2
                exit 1
            fi
            if ! hermes_is_available; then
                echo "ERROR: Tailscale configurator healthcheck: Hermes endpoints are unavailable" >&2
                exit 1
            fi
            if ! serve_is_exact 1; then
                echo "ERROR: Tailscale configurator healthcheck: Serve configuration is not exact" >&2
                exit 1
            fi
            ;;
        *)
            if ! [ -S "${socket}" ]; then
                echo "ERROR: Tailscale configurator healthcheck: socket unavailable" >&2
                exit 1
            fi
            if ! service_host_is_active 0; then
                echo "ERROR: Tailscale configurator healthcheck: Service host routes are not active" >&2
                exit 1
            fi
            if ! serve_is_exact 0; then
                echo "ERROR: Tailscale configurator healthcheck: Serve configuration is not exact" >&2
                exit 1
            fi
            ;;
    esac
    exit
fi

# Only the long-running reconciler owns Service teardown. A timed-out Docker
# healthcheck runs this same script and must never withdraw healthy routes.
trap shutdown_services 1 2 15

while [ "${remaining}" -gt 0 ]; do
    if [ -S "${socket}" ] && ts status --json >/dev/null 2>&1; then
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
configuration_ready=1
if ! wait_for_exact_services "${include_hermes}"; then
    echo "ERROR: Tailscale Services do not have the exact HTTPS listeners." >&2
    # A fresh child tailnet can accept the tagged host before its Serve
    # configuration and route advertisements converge. Keep the reconciler
    # alive in normal operation so its next pass can repair that state instead
    # of relying on a container restart (which also resets the health window).
    configuration_ready=0
fi

remaining=120
route_repair_remaining=0
routes_ready=1
while [ "${remaining}" -gt 0 ]; do
    if service_host_is_active "${include_hermes}"; then
        break
    fi
    if [ "${route_repair_remaining}" -le 0 ] \
        && service_host_is_approved "${include_hermes}"; then
        configure_services "${include_hermes}"
        wait_for_exact_services "${include_hermes}" || exit 1
        route_repair_remaining=30
    fi
    sleep 2
    remaining=$((remaining - 2))
    route_repair_remaining=$((route_repair_remaining - 2))
done
if [ "${remaining}" -le 0 ]; then
    echo "ERROR: Tailscale has not approved and activated the selected Service hosts;" \
        "verify auto-approval and grant tag:vonk-gateway TCP 443 access" \
        "to every hosted Service." >&2
    # Do not exit the long-running reconciler here. Tailscale may publish the
    # service-host route after the initial bounded wait; the reconciler below
    # will re-advertise it once the child policy is visible. TS_CONFIGURE_ONCE
    # remains fail-closed for one-shot validation and acceptance checks.
    routes_ready=0
fi

if [ "${TS_CONFIGURE_ONCE:-0}" = "1" ]; then
    [ "${configuration_ready}" -eq 1 ] && [ "${routes_ready}" -eq 1 ] || exit 1
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
    # Re-advertise when control has mapped the TailVIPs but tailscaled has not
    # activated their PrimaryRoutes. This repairs the userspace-host race seen
    # after a container is created or restored.
    if ! service_host_is_active "${desired_hermes}"; then
        if service_host_is_approved "${desired_hermes}"; then
            configure_services "${desired_hermes}"
            wait_for_exact_services "${desired_hermes}" || exit 1
        fi
        continue
    fi
done

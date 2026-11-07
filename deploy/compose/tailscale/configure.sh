#!/bin/sh
set -eu

socket=${TS_SOCKET_PATH:-/var/run/tailscale/tailscaled.sock}
remaining=120
route_wait_seconds=${TS_ROUTE_WAIT_SECONDS:-300}
# Service-host mappings are sufficient for local readiness. An acceptance run
# without an independent tailnet client can opt out of service-host publication
# entirely; it still validates gateway liveness and exact Serve configuration.
require_primary_routes=${TS_REQUIRE_PRIMARY_ROUTES:-1}
require_service_host=${TS_REQUIRE_SERVICE_HOST:-1}
ephemeral=${VONK_TAILSCALE_EPHEMERAL:-false}
case "${require_primary_routes}" in
    0|1) ;;
    *) echo "ERROR: TS_REQUIRE_PRIMARY_ROUTES must be 0 or 1." >&2; exit 64 ;;
esac
case "${require_service_host}" in
    0|1) ;;
    *) echo "ERROR: TS_REQUIRE_SERVICE_HOST must be 0 or 1." >&2; exit 64 ;;
esac
case "${ephemeral}" in
    true|false) ;;
    *) echo "ERROR: VONK_TAILSCALE_EPHEMERAL must be true or false." >&2; exit 64 ;;
esac
if [ "${require_service_host}" = "0" ] && [ "${ephemeral}" != "true" ]; then
    echo "ERROR: TS_REQUIRE_SERVICE_HOST=0 is limited to ephemeral acceptance." >&2
    exit 64
fi
reconcile_interval=${TS_RECONCILE_INTERVAL_SECONDS:-30}
control_service=${VONK_TAILSCALE_CONTROL_SERVICE:-svc:vonk-forge}

validate_service_name() {
    printf '%s\n' "$1" \
        | grep -Eq '^svc:[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'
}

validate_service_name "${control_service}" \
    || { echo "ERROR: Tailscale Service names are invalid." >&2; exit 64; }

default_service_map=$(printf \
    '{"version":"0.0.1","services":{"%s":{"endpoints":{"tcp:443":"http://caddy:8080"}}}}' \
    "${control_service}")

scratch_root=${TMPDIR:-/tmp}
runtime_dir=$(mktemp -d "${scratch_root%/}/vonk-tailscale.XXXXXX")
empty_service_map=${runtime_dir}/empty-service-map.json
printf '%s\n' '{"version":"0.0.1","services":{}}' >"${empty_service_map}"
cleanup() {
    case "${runtime_dir:-}" in
        "${scratch_root%/}"/vonk-tailscale.*)
            rm -rf -- "${runtime_dir:?}"
            ;;
    esac
}
trap cleanup 0

ts() {
    tailscale --socket="${socket}" "$@"
}

withdraw_all_services() {
    # A stopped gateway must not remain registered as a candidate host for the
    # fixed Service VIPs. Drain first, then atomically clear the full map and
    # AdvertiseServices preference before tailscaled itself is stopped.
    ts serve drain "${control_service}" >/dev/null 2>&1 || true
    ts serve set-config --all "${empty_service_map}" >/dev/null 2>&1 || true
}

canonicalize_service_map() {
    service_map=$1
    destination=$2
    # `tailscale serve get-config --all` emits a JSON object whose service
    # member is a Go map. Its member order is deliberately unspecified and can
    # differ across otherwise identical processes or Tailscale versions. Keep
    # the document shape and every service detail exact, but compare the
    # individual service entries after sorting them by their complete value.
    service_entries=$(printf '%s\n' "${service_map}" | sed -n \
        -e 's/^{"version":"0.0.1","services":{\(.*\)}}$/\1/p' \
        -e 's/^{"services":{\(.*\)},"version":"0.0.1"}$/\1/p')
    [ -n "${service_entries}" ] || return 1
    printf '%s\n' "${service_entries}" \
        | sed 's/},"/}\
"/g' \
        | sort >"${destination}"
}

service_map_is_exact() {
    actual_map=$1
    expected_map=$2
    canonicalize_service_map \
        "${actual_map}" "${runtime_dir}/tailscale-serve-config.actual" \
        && canonicalize_service_map \
            "${expected_map}" "${runtime_dir}/tailscale-serve-config.expected" \
        && cmp -s \
            "${runtime_dir}/tailscale-serve-config.actual" \
            "${runtime_dir}/tailscale-serve-config.expected"
}

report_serve_mismatch() {
    # Serve configuration contains only validated Service names and local
    # upstreams. Include bounded diagnostics in Docker's health log so a future
    # Tailscale representation change is actionable instead of looking like a
    # hung deployment.
    printf 'ERROR: actual Tailscale Serve config: ' >&2
    if [ -r "${runtime_dir}/tailscale-serve-config.compact" ]; then
        cut -c 1-4096 "${runtime_dir}/tailscale-serve-config.compact" >&2
    else
        printf '<unavailable>\n' >&2
    fi
    printf 'ERROR: actual Tailscale Serve status: ' >&2
    if [ -r "${runtime_dir}/tailscale-serve-status.compact" ]; then
        cut -c 1-4096 "${runtime_dir}/tailscale-serve-status.compact" >&2
    else
        printf '<unavailable>\n' >&2
    fi
}

shutdown_services() {
    trap - 1 2 15
    if [ -S "${socket}" ]; then
        withdraw_all_services
    fi
    exit 0
}

serve_is_exact() {
    ts serve status --json >"${runtime_dir}/tailscale-serve-status.json"
    ts serve get-config --all >"${runtime_dir}/tailscale-serve-config.json"
    tr -d '[:space:]' \
        <"${runtime_dir}/tailscale-serve-status.json" \
        >"${runtime_dir}/tailscale-serve-status.compact"
    tr -d '[:space:]' \
        <"${runtime_dir}/tailscale-serve-config.json" \
        >"${runtime_dir}/tailscale-serve-config.compact"

    actual_service_map=$(cat "${runtime_dir}/tailscale-serve-config.compact")
    # Tailscale reports an unapproved/pending service advertisement as the
    # advisory `advertised:false` field in get-config output.  Disposable
    # acceptance without an independent tailnet client deliberately permits
    # that control-plane state; it still requires the exact local HTTPS
    # endpoint map and status below.  Production keeps the advertised bit
    # strict because it must prove that the Service is actually published.
    comparable_service_map=${actual_service_map}
    if [ "${require_service_host}" = "0" ] && [ "${ephemeral}" = "true" ]; then
        comparable_service_map=$(printf '%s' "${comparable_service_map}" \
            | sed -e 's/,"advertised":false//g' \
                -e 's/"advertised":false,//g')
    fi
    grep -Fq "\"${control_service}\":{\"TCP\":{\"443\":{\"HTTPS\":true}}" \
        "${runtime_dir}/tailscale-serve-status.compact" \
        && ! grep -Fq '"443":{"HTTP":true}' "${runtime_dir}/tailscale-serve-status.compact" \
        && ! grep -Fq '"TCPForward"' "${runtime_dir}/tailscale-serve-status.compact" \
        && service_map_is_exact \
            "${comparable_service_map}" "${default_service_map}"
}

serve_is_pending_ephemeral_acceptance() {
    # A disposable child tailnet with no independent client can accept the
    # tagged gateway before it accepts any Service advertisement. Newer
    # tailscaled releases represent that state by omitting the whole local
    # service map instead of retaining entries with `advertised:false`.
    # Accept only that exact empty representation, and only under the existing
    # explicit ephemeral/no-service-host acceptance boundary. Production and
    # external-client acceptance remain strict.
    pending_service_map=$(cat "${runtime_dir}/tailscale-serve-config.compact")
    pending_serve_status=$(cat "${runtime_dir}/tailscale-serve-status.compact")
    [ "${require_service_host}" = "0" ] \
        && [ "${ephemeral}" = "true" ] \
        && [ "${pending_service_map}" = '{"version":"0.0.1"}' ] \
        && [ "${pending_serve_status}" = '{}' ]
}

configure_services() {
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
    advertise_services
}

advertise_services() {
    ts serve advertise "${control_service}" >/dev/null
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
    service_has_mapped_addresses "${control_service}"
}

reconfigure_approved_services() {
    repaired=1
    if service_has_mapped_addresses "${control_service}"; then
        ts serve drain "${control_service}" >/dev/null 2>&1 || true
        ts serve --service="${control_service}" --https=443 http://caddy:8080 >/dev/null
        ts serve advertise "${control_service}" >/dev/null
        repaired=0
    fi
    return "${repaired}"
}

service_has_primary_routes() {
    service_name=$1
    service_has_mapped_addresses "${service_name}" || return 1
    previous_ifs=$IFS
    IFS=,
    # shellcheck disable=SC2086 # the address list is split into words on purpose
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
    ts status --json >"${runtime_dir}/tailscale-status.json" || return 1
    tr -d '[:space:]' \
        <"${runtime_dir}/tailscale-status.json" \
        >"${runtime_dir}/tailscale-status.compact"

    # services/* capabilities authorize this node as a client of a TailVIP;
    # they do not prove that control has approved this tagged node to host it.
    # service-host maps approved names to TailVIPs. PrimaryRoutes is generic
    # subnet-route state and is not consistently populated for Service hosts
    # (tailscale/tailscale#19666). Keep it as an opt-in external acceptance
    # assertion; local healthchecks validate the documented service-host map
    # and exact Serve configuration instead.
    service_hosts=${runtime_dir}/tailscale-service-hosts.compact
    extract_json_array service-host \
        "${runtime_dir}/tailscale-status.compact" "${service_hosts}" \
        || return 1
    if [ "${require_primary_routes}" = "1" ]; then
        primary_routes=${runtime_dir}/tailscale-primary-routes.compact
        extract_json_array PrimaryRoutes \
            "${runtime_dir}/tailscale-status.compact" "${primary_routes}" \
            || return 1
        service_has_primary_routes "${control_service}"
        return
    fi
    service_host_is_approved
}

if [ "${TS_HEALTHCHECK_ONLY:-0}" = "1" ]; then
    if ! [ -S "${socket}" ]; then
        echo "ERROR: Tailscale configurator healthcheck: socket unavailable" >&2
        exit 1
    fi
    if [ "${require_service_host}" = "1" ] \
        && ! service_host_is_active; then
        echo "ERROR: Tailscale configurator healthcheck: Service host routes are not active" >&2
        exit 1
    fi
    if ! serve_is_exact \
        && ! serve_is_pending_ephemeral_acceptance; then
        echo "ERROR: Tailscale configurator healthcheck: Serve configuration is not exact" >&2
        report_serve_mismatch
        exit 1
    fi
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

if ! serve_is_exact; then
    configure_services
fi
configuration_ready=1
if ! wait_for_exact_services; then
    echo "ERROR: Tailscale Services do not have the exact HTTPS listeners." >&2
    # A fresh child tailnet can accept the tagged host before its Serve
    # configuration and route advertisements converge. Keep the reconciler
    # alive in normal operation so its next pass can repair that state instead
    # of relying on a container restart (which also resets the health window).
    configuration_ready=0
fi

routes_ready=1
if [ "${require_service_host}" = "1" ]; then
    remaining=${route_wait_seconds}
    route_repair_remaining=0
    while [ "${remaining}" -gt 0 ]; do
        if service_host_is_active; then
            break
        fi
        if [ "${route_repair_remaining}" -le 0 ]; then
            # The first advertisement can race tailnet policy propagation. A
            # pending advertisement is not represented by service-host, so retry
            # the exact local advertisement until approval appears. Once control
            # has mapped the TailVIPs but tailscaled has not activated their
            # PrimaryRoutes, replace the exact Serve map: an idempotent
            # `serve advertise` is insufficient for the stale-host state where
            # local Serve config is exact but the daemon retained an advertisement
            # without installing its primary routes. The policy remains the only
            # authority that approves the host.
            if reconfigure_approved_services; then
                wait_for_exact_services || true
            else
                advertise_services || true
            fi
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
fi

if [ "${TS_CONFIGURE_ONCE:-0}" = "1" ]; then
    [ "${configuration_ready}" -eq 1 ] && [ "${routes_ready}" -eq 1 ] || exit 1
    exit 0
fi

# Persist as a small reconciler. If gateway state is restored or replaced while
# this Compose project stays up, a missing or downgraded listener is repaired
# without an operator having to recreate this container.
while :; do
    # Interruptible, so the shutdown trap runs at once and not after the sleep.
    sleep "${reconcile_interval}" & wait $!
    if ! serve_is_exact; then
        configure_services
        wait_for_exact_services || exit 1
    fi
    # Re-advertise while approval is still pending. Once control has mapped the
    # TailVIPs but tailscaled has not activated their PrimaryRoutes, replace the
    # exact Serve map: an idempotent `serve advertise` is insufficient for the
    # stale-host state where local Serve config is exact but the daemon retained
    # an advertisement without installing its primary routes. Replacing the map
    # forces the same configuration path as a fresh gateway without Docker
    # access or a container restart. The policy remains the only authority that
    # approves the host.
    if [ "${require_service_host}" = "1" ] \
        && ! service_host_is_active; then
        if reconfigure_approved_services; then
            wait_for_exact_services || true
        else
            # A pending advertisement is not represented by service-host, so
            # keep retrying the exact local advertisement until policy approval
            # appears.
            advertise_services || true
        fi
        continue
    fi
done

#!/bin/sh
set -eu

valid_generation() {
  printf '%s\n' "$1" | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
}

read_hostname_authority() {
  [ ! -L "$VONK_CONTROL_HOSTNAME_FILE" ] \
    && [ -f "$VONK_CONTROL_HOSTNAME_FILE" ] \
    && [ -r "$VONK_CONTROL_HOSTNAME_FILE" ] \
    && [ -s "$VONK_CONTROL_HOSTNAME_FILE" ] \
    && [ "$(wc -l < "$VONK_CONTROL_HOSTNAME_FILE")" -eq 1 ] \
    && [ "$(wc -c < "$VONK_CONTROL_HOSTNAME_FILE")" -le 300 ] \
    && cat "$VONK_CONTROL_HOSTNAME_FILE"
}

parse_hostname_authority() {
  raw_hostname_authority=$1
  saved_authority_ifs=$IFS
  IFS=' '
  set -- $raw_hostname_authority
  IFS=$saved_authority_ifs
  [ "$#" -eq 2 ] && valid_generation "$1" || return 1
  parsed_hostname_generation=$1
  parsed_control_hostname=$2
}

health_mode=0
if [ "${1:-}" = "health" ]; then
  [ "$#" -eq 1 ] || exit 1
  : "${VONK_CONTROL_HOSTNAME_FILE:?set VONK_CONTROL_HOSTNAME_FILE}"
  hostname_authority=$(read_hostname_authority) || exit 1
  parse_hostname_authority "$hostname_authority" || exit 1
  export VONK_CONTROL_HOSTNAME_GENERATION=$parsed_hostname_generation
  health_mode=1
fi

browser_hostname_pending=0
if [ -n "${VONK_CONTROL_HOSTNAME_FILE:-}" ]; then
  if [ -n "${VONK_CONTROL_HOSTNAME:-}" ]; then
    printf 'Vonk Forge Caddy browser hostname authority is ambiguous\n' >&2
    exit 64
  fi
  # A persisted record is never startup authority. Only a record observed after
  # this Caddy process staged agent-only supplies a fresh configurator generation.
  browser_hostname_pending=1
  if [ -n "${VONK_CONTROL_HOSTNAME_GENERATION:-}" ]; then
    if ! valid_generation "$VONK_CONTROL_HOSTNAME_GENERATION"; then
      printf 'Vonk Forge Caddy browser hostname generation is invalid\n' >&2
      exit 64
    fi
    if hostname_authority=$(read_hostname_authority); then
      if parse_hostname_authority "$hostname_authority" \
        && [ "$parsed_hostname_generation" = "$VONK_CONTROL_HOSTNAME_GENERATION" ]; then
        VONK_CONTROL_HOSTNAME=$parsed_control_hostname
        browser_hostname_pending=0
      fi
    fi
  fi
fi
if [ "$browser_hostname_pending" -eq 0 ]; then
  : "${VONK_CONTROL_HOSTNAME:?set VONK_CONTROL_HOSTNAME or VONK_CONTROL_HOSTNAME_FILE}"
fi
: "${VONK_AGENT_ENROLL_HOSTNAME:?set VONK_AGENT_ENROLL_HOSTNAME}"
: "${VONK_AGENT_HOSTNAME:?set VONK_AGENT_HOSTNAME}"
: "${VONK_BACKEND_PORT:?set VONK_BACKEND_PORT}"

control_hostname_poll_interval=2
if [ "${VONK_CADDY_HANDOFF_TEST_MODE:-0}" = "1" ]; then
  control_hostname_poll_interval=${VONK_CONTROL_HOSTNAME_POLL_INTERVAL:-1}
  case "$control_hostname_poll_interval" in
    "" | *[!0-9]*)
      printf 'Vonk Forge Caddy test poll interval is invalid\n' >&2
      exit 64
      ;;
  esac
  if [ "$control_hostname_poll_interval" -lt 1 ] \
    || [ "$control_hostname_poll_interval" -gt 5 ]; then
    printf 'Vonk Forge Caddy test poll interval is invalid\n' >&2
    exit 64
  fi
elif [ -n "${VONK_CONTROL_HOSTNAME_POLL_INTERVAL:-}" ]; then
  printf 'Vonk Forge Caddy test poll interval requires test mode\n' >&2
  exit 64
fi

case "$VONK_BACKEND_PORT" in
  "" | *[!0-9]*)
    printf 'VONK_BACKEND_PORT must be an integer from 1 through 65535\n' >&2
    exit 64
    ;;
esac
if [ "$VONK_BACKEND_PORT" -lt 1 ] || [ "$VONK_BACKEND_PORT" -gt 65535 ]; then
  printf 'VONK_BACKEND_PORT must be an integer from 1 through 65535\n' >&2
  exit 64
fi

normalize_hostname() {
  hostname=$1
  case "$hostname" in
    *[!A-Za-z0-9.-]*)
      printf 'Vonk Forge Caddy SNI hostname is invalid\n' >&2
      exit 64
      ;;
  esac
  normalized=$(printf '%s' "$hostname" | tr '[:upper:]' '[:lower:]')
  normalized=${normalized%.}
  case "$normalized" in
    "" | .* | *..* | *.)
      printf 'Vonk Forge Caddy SNI hostname is invalid\n' >&2
      exit 64
      ;;
  esac
  saved_ifs=$IFS
  IFS=.
  set -- $normalized
  IFS=$saved_ifs
  for label in "$@"; do
    case "$label" in
      -* | *-)
        printf 'Vonk Forge Caddy SNI hostname is invalid\n' >&2
        exit 64
        ;;
    esac
  done
  printf '%s' "$normalized"
}

enrollment_hostname=$(normalize_hostname "$VONK_AGENT_ENROLL_HOSTNAME")
agent_hostname=$(normalize_hostname "$VONK_AGENT_HOSTNAME")
if [ "$enrollment_hostname" = "$agent_hostname" ]; then
  printf 'Vonk Forge Caddy SNI hostnames must be distinct\n' >&2
  exit 64
fi
export VONK_AGENT_ENROLL_HOSTNAME=$enrollment_hostname
export VONK_AGENT_HOSTNAME=$agent_hostname
if [ "$browser_hostname_pending" -eq 0 ]; then
  control_hostname=$(normalize_hostname "$VONK_CONTROL_HOSTNAME")
  case "$control_hostname" in
    vonk-forge.*.ts.net) ;;
    *)
      printf 'Vonk Forge browser hostname must be vonk-forge.<tailnet-name>.ts.net\n' >&2
      exit 64
      ;;
  esac
  if [ "$control_hostname" = "$enrollment_hostname" ] \
    || [ "$control_hostname" = "$agent_hostname" ]; then
    printf 'Vonk Forge Caddy SNI hostnames must be distinct\n' >&2
    exit 64
  fi
  export VONK_CONTROL_HOSTNAME=$control_hostname
fi

if [ "$health_mode" -eq 1 ]; then
  wget -q --spider -T 3 http://127.0.0.1:2019/healthz \
    && wget -q --spider -T 3 \
      --header="Host: ${control_hostname}" \
      http://127.0.0.1:8080/healthz
  exit $?
fi

for required_file in \
  /run/secrets/controller-server-certificate \
  /run/secrets/controller-server-key \
  /run/secrets/agent-ca-certificate \
  /run/secrets/agent-proxy-auth \
  /run/secrets/management-cidrs
do
  if [ -L "$required_file" ] || [ ! -f "$required_file" ] || [ ! -r "$required_file" ] || [ ! -s "$required_file" ]; then
    printf 'Vonk Forge Caddy required runtime file is unavailable\n' >&2
    exit 1
  fi
done

if ! invalid_proxy_auth_bytes=$(LC_ALL=C tr -d 'A-Za-z0-9_\r\n-' < /run/secrets/agent-proxy-auth | wc -c); then
  printf 'Vonk Forge Caddy proxy authentication secret is unavailable\n' >&2
  exit 1
fi
if [ "$invalid_proxy_auth_bytes" -ne 0 ]; then
  printf 'Vonk Forge Caddy proxy authentication secret is invalid\n' >&2
  exit 1
fi
if ! proxy_auth_raw=$(cat /run/secrets/agent-proxy-auth); then
  printf 'Vonk Forge Caddy proxy authentication secret is unavailable\n' >&2
  exit 1
fi

carriage_return=$(printf '\r')
line_feed='
'
while :; do
  case "$proxy_auth_raw" in
    *"$carriage_return") proxy_auth_raw=${proxy_auth_raw%"$carriage_return"} ;;
    *"$line_feed") proxy_auth_raw=${proxy_auth_raw%"$line_feed"} ;;
    *) break ;;
  esac
done
proxy_auth=$proxy_auth_raw
case "$proxy_auth" in
  "" | *[!A-Za-z0-9_-]*)
    printf 'Vonk Forge Caddy proxy authentication secret is invalid\n' >&2
    exit 1
    ;;
esac
if [ "${#proxy_auth}" -lt 32 ]; then
  printf 'Vonk Forge Caddy proxy authentication secret is invalid\n' >&2
  exit 1
fi
proxy_header=/tmp/vonk-agent-proxy-auth.caddy
if [ -L "$proxy_header" ] || { [ -e "$proxy_header" ] && [ ! -f "$proxy_header" ]; }; then
  printf 'Vonk Forge Caddy proxy authentication projection is unsafe\n' >&2
  exit 1
fi
temporary=$(mktemp /tmp/.vonk-agent-proxy-auth.XXXXXX)
trap 'rm -f "$temporary"' EXIT HUP INT TERM
printf 'header_up X-Vonk-Agent-Proxy-Auth "%s"\n' "$proxy_auth" > "$temporary"
chmod 0400 "$temporary"
mv -f "$temporary" "$proxy_header"
trap - EXIT HUP INT TERM
unset proxy_auth proxy_auth_raw invalid_proxy_auth_bytes

if ! invalid_cidr_bytes=$(LC_ALL=C tr -d '0-9A-Fa-f:./ \t\r\n' < /run/secrets/management-cidrs | wc -c); then
  printf 'Vonk Forge Caddy management CIDRs are unavailable\n' >&2
  exit 1
fi
if [ "$invalid_cidr_bytes" -ne 0 ]; then
  printf 'Vonk Forge Caddy management CIDRs are invalid\n' >&2
  exit 1
fi
management_cidrs=
for cidr in $(cat /run/secrets/management-cidrs); do
  case "$cidr" in
    */*) ;;
    *)
      printf 'Vonk Forge Caddy management CIDRs are invalid\n' >&2
      exit 1
      ;;
  esac
  management_cidrs="$management_cidrs $cidr"
done
management_cidrs=${management_cidrs# }
if [ -z "$management_cidrs" ]; then
  printf 'Vonk Forge Caddy management CIDRs are invalid\n' >&2
  exit 1
fi
export VONK_MANAGEMENT_CIDRS=$management_cidrs

runtime_root=/run/vonk-caddy
runtime_caddy=$runtime_root/caddy
if [ -L "$runtime_root" ] || [ ! -d "$runtime_root" ] || [ ! -w "$runtime_root" ]; then
  printf 'Vonk Forge Caddy runtime directory is unavailable\n' >&2
  exit 1
fi
if [ "$(stat -c '%u:%g:%a' "$runtime_root")" != "10000:10000:700" ]; then
  printf 'Vonk Forge Caddy runtime directory is unsafe\n' >&2
  exit 1
fi
if [ -L /usr/bin/caddy ] || [ ! -f /usr/bin/caddy ] || [ ! -x /usr/bin/caddy ]; then
  printf 'Vonk Forge Caddy image binary is unavailable\n' >&2
  exit 1
fi
if [ -L "$runtime_caddy" ] || { [ -e "$runtime_caddy" ] && [ ! -f "$runtime_caddy" ]; }; then
  printf 'Vonk Forge Caddy runtime binary target is unsafe\n' >&2
  exit 1
fi
runtime_temporary=$(mktemp "$runtime_root/.caddy.XXXXXX")
trap 'rm -f "$runtime_temporary"' EXIT HUP INT TERM
cp /usr/bin/caddy "$runtime_temporary"
chmod 0500 "$runtime_temporary"
mv -f "$runtime_temporary" "$runtime_caddy"
trap - EXIT HUP INT TERM

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

if [ "$browser_hostname_pending" -eq 0 ] \
  && [ -z "${VONK_CONTROL_HOSTNAME_FILE:-}" ]; then
  exec "$runtime_caddy" run --config /run/vonk-runtime/Caddyfile --adapter caddyfile
fi

source_caddyfile=/run/vonk-runtime/Caddyfile
selected_caddyfile=$source_caddyfile
if [ "$browser_hostname_pending" -eq 1 ]; then
  agent_caddyfile=$runtime_root/Caddyfile.agent-only
  if [ ! -f "$source_caddyfile" ] \
    || [ "$(grep -Fxc '# BEGIN PRIVATE BROWSER EDGE' "$source_caddyfile")" -ne 1 ] \
    || [ "$(grep -Fxc '# END PRIVATE BROWSER EDGE' "$source_caddyfile")" -ne 1 ]; then
    printf 'Vonk Forge Caddy staged configuration is invalid\n' >&2
    exit 1
  fi
  agent_temporary=$(mktemp "$runtime_root/.Caddyfile.agent-only.XXXXXX")
  trap 'rm -f "$agent_temporary"' EXIT HUP INT TERM
  if ! awk '
    $0 == "# BEGIN PRIVATE BROWSER EDGE" { hidden = 1; next }
    $0 == "# END PRIVATE BROWSER EDGE" { hidden = 0; found = 1; next }
    !hidden { print }
    END { if (hidden || !found) exit 1 }
  ' "$source_caddyfile" >"$agent_temporary"; then
    printf 'Vonk Forge Caddy staged configuration is invalid\n' >&2
    exit 1
  fi
  chmod 0400 "$agent_temporary"
  mv -f "$agent_temporary" "$agent_caddyfile"
  trap - EXIT HUP INT TERM
  selected_caddyfile=$agent_caddyfile
fi

hostname_authority_snapshot() {
  if authority=$(read_hostname_authority); then
    printf '%s:%s' "$(stat -c '%d:%i' "$VONK_CONTROL_HOSTNAME_FILE")" "$authority"
  else
    printf 'unavailable'
  fi
}

active_hostname_authority=
staged_hostname_snapshot=
if [ "$browser_hostname_pending" -eq 1 ]; then
  staged_hostname_snapshot=$(hostname_authority_snapshot)
elif [ -n "${VONK_CONTROL_HOSTNAME_FILE:-}" ]; then
  active_hostname_authority=$(read_hostname_authority)
fi
"$runtime_caddy" run --config "$selected_caddyfile" --adapter caddyfile &
caddy_pid=$!
terminate_caddy() {
  kill -TERM "$caddy_pid" 2>/dev/null || true
  wait "$caddy_pid" 2>/dev/null || true
}
trap 'terminate_caddy; exit 0' TERM INT
trap 'terminate_caddy; exit 1' HUP

while kill -0 "$caddy_pid" 2>/dev/null; do
  if [ "$browser_hostname_pending" -eq 1 ]; then
    observed_hostname_snapshot=$(hostname_authority_snapshot)
    if [ "$observed_hostname_snapshot" != "$staged_hostname_snapshot" ]; then
      staged_hostname_snapshot=$observed_hostname_snapshot
      if observed_hostname_authority=$(read_hostname_authority); then
        if parse_hostname_authority "$observed_hostname_authority"; then
          terminate_caddy
          trap - TERM INT HUP
          unset VONK_CONTROL_HOSTNAME
          export VONK_CONTROL_HOSTNAME_GENERATION=$parsed_hostname_generation
          exec /bin/sh "$0"
        fi
      fi
    fi
  else
    observed_hostname_authority=
    if authority=$(read_hostname_authority); then
      observed_hostname_authority=$authority
    fi
    if [ "$observed_hostname_authority" != "$active_hostname_authority" ]; then
      terminate_caddy
      trap - TERM INT HUP
      unset VONK_CONTROL_HOSTNAME
      if parse_hostname_authority "$observed_hostname_authority"; then
        export VONK_CONTROL_HOSTNAME_GENERATION=$parsed_hostname_generation
      else
        unset VONK_CONTROL_HOSTNAME_GENERATION
      fi
      exec /bin/sh "$0"
    fi
  fi
  sleep "$control_hostname_poll_interval"
done
wait "$caddy_pid"

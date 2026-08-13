#!/bin/sh
set -eu

browser_hostname_pending=0
if [ -n "${VONK_CONTROL_HOSTNAME_FILE:-}" ]; then
  if [ -n "${VONK_CONTROL_HOSTNAME:-}" ]; then
    printf 'Vonk Forge Caddy browser hostname authority is ambiguous\n' >&2
    exit 64
  fi
  if [ ! -e "$VONK_CONTROL_HOSTNAME_FILE" ] \
    && [ ! -L "$VONK_CONTROL_HOSTNAME_FILE" ]; then
    browser_hostname_pending=1
  elif [ -L "$VONK_CONTROL_HOSTNAME_FILE" ] \
    || [ ! -f "$VONK_CONTROL_HOSTNAME_FILE" ] \
    || [ ! -r "$VONK_CONTROL_HOSTNAME_FILE" ] \
    || [ ! -s "$VONK_CONTROL_HOSTNAME_FILE" ] \
    || [ "$(wc -l < "$VONK_CONTROL_HOSTNAME_FILE")" -ne 1 ] \
    || [ "$(wc -c < "$VONK_CONTROL_HOSTNAME_FILE")" -gt 254 ]; then
    printf 'Vonk Forge Caddy browser hostname file is invalid\n' >&2
    exit 64
  elif ! VONK_CONTROL_HOSTNAME=$(cat "$VONK_CONTROL_HOSTNAME_FILE"); then
    printf 'Vonk Forge Caddy browser hostname file is unavailable\n' >&2
    exit 1
  fi
fi
if [ "$browser_hostname_pending" -eq 0 ]; then
  : "${VONK_CONTROL_HOSTNAME:?set VONK_CONTROL_HOSTNAME or VONK_CONTROL_HOSTNAME_FILE}"
fi
: "${VONK_AGENT_ENROLL_HOSTNAME:?set VONK_AGENT_ENROLL_HOSTNAME}"
: "${VONK_AGENT_HOSTNAME:?set VONK_AGENT_HOSTNAME}"
: "${VONK_BACKEND_PORT:?set VONK_BACKEND_PORT}"

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

active_control_hostname=${VONK_CONTROL_HOSTNAME:-}
"$runtime_caddy" run --config "$selected_caddyfile" --adapter caddyfile &
caddy_pid=$!
terminate_caddy() {
  kill -TERM "$caddy_pid" 2>/dev/null || true
  wait "$caddy_pid" 2>/dev/null || true
}
trap 'terminate_caddy; exit 0' TERM INT
trap 'terminate_caddy; exit 1' HUP

while kill -0 "$caddy_pid" 2>/dev/null; do
  observed_control_hostname=
  if [ -f "$VONK_CONTROL_HOSTNAME_FILE" ] \
    && [ ! -L "$VONK_CONTROL_HOSTNAME_FILE" ]; then
    observed_control_hostname=$(cat "$VONK_CONTROL_HOSTNAME_FILE")
  fi
  if [ "$observed_control_hostname" != "$active_control_hostname" ]; then
    terminate_caddy
    trap - TERM INT HUP
    exec /bin/sh "$0"
  fi
  sleep 2
done
wait "$caddy_pid"

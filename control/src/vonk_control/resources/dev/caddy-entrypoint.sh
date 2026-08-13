#!/bin/sh
set -eu

: "${VONK_CONTROL_HOSTNAME:?set VONK_CONTROL_HOSTNAME}"
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

control_hostname=$(normalize_hostname "$VONK_CONTROL_HOSTNAME")
case "$control_hostname" in
  vonk-forge.*.ts.net) ;;
  *)
    printf 'Vonk Forge browser hostname must be vonk-forge.<tailnet-name>.ts.net\n' >&2
    exit 64
    ;;
esac
enrollment_hostname=$(normalize_hostname "$VONK_AGENT_ENROLL_HOSTNAME")
agent_hostname=$(normalize_hostname "$VONK_AGENT_HOSTNAME")
if [ "$control_hostname" = "$enrollment_hostname" ] \
  || [ "$control_hostname" = "$agent_hostname" ] \
  || [ "$enrollment_hostname" = "$agent_hostname" ]; then
  printf 'Vonk Forge Caddy SNI hostnames must be distinct\n' >&2
  exit 64
fi
export VONK_CONTROL_HOSTNAME=$control_hostname
export VONK_AGENT_ENROLL_HOSTNAME=$enrollment_hostname
export VONK_AGENT_HOSTNAME=$agent_hostname

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

if [ "$#" -eq 0 ]; then
  set -- "$runtime_caddy" run --config /run/vonk-runtime/Caddyfile --adapter caddyfile
fi
exec "$@"

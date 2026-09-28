#!/bin/sh
set -eu

: "${VONK_CONTROL_HOSTNAME:?set VONK_CONTROL_HOSTNAME}"

normalize_hostname() {
  hostname=$1
  case "$hostname" in
    *[!A-Za-z0-9.-]*)
      echo "Vonk Forge Caddy SNI hostname is invalid: $hostname" >&2
      exit 64
      ;;
  esac
  normalized=$(printf '%s' "$hostname" | tr '[:upper:]' '[:lower:]')
  normalized=${normalized%.}
  case "$normalized" in
    "" | .* | *..* | *.)
      echo "Vonk Forge Caddy SNI hostname is invalid: $hostname" >&2
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
        echo "Vonk Forge Caddy SNI hostname is invalid: $hostname" >&2
        exit 64
        ;;
    esac
  done
  printf '%s' "$normalized"
}

# One control hostname names the whole controller: the enrollment, agent, and
# registry SNI names are fixed prefixes of it (the installer's certificate
# carries exactly these names).
control_hostname=$(normalize_hostname "$VONK_CONTROL_HOSTNAME")
export VONK_CONTROL_HOSTNAME="$control_hostname"
export VONK_AGENT_ENROLL_HOSTNAME="enroll.$control_hostname"
export VONK_AGENT_HOSTNAME="agents.$control_hostname"
export VONK_REGISTRY_HOSTNAME="registry.$control_hostname"

for required_file in \
  /run/secrets/controller-server-certificate \
  /run/secrets/controller-server-key \
  /run/secrets/agent-client-ca
do
  if [ -L "$required_file" ] || [ ! -f "$required_file" ] || [ ! -r "$required_file" ] || [ ! -s "$required_file" ]; then
    echo "Vonk Forge Caddy required runtime file is unavailable" >&2
    exit 1
  fi
done

if ! invalid_proxy_auth_bytes=$(LC_ALL=C tr -d 'A-Za-z0-9_\r\n-' < /run/secrets/agent-proxy-auth | wc -c); then
  echo "Vonk Forge Caddy proxy authentication secret is unavailable" >&2
  exit 1
fi
if [ "$invalid_proxy_auth_bytes" -ne 0 ]; then
  echo "Vonk Forge Caddy proxy authentication secret must be one base64url-like token of at least 32 characters" >&2
  exit 1
fi
if ! proxy_auth_raw=$(cat /run/secrets/agent-proxy-auth); then
  echo "Vonk Forge Caddy proxy authentication secret is unavailable" >&2
  exit 1
fi

# Command substitution removes final LF bytes. Remove any remaining CR/LF
# terminators explicitly, while preserving (and therefore rejecting) them if
# they occur within the token.
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
    echo "Vonk Forge Caddy proxy authentication secret must be one base64url-like token of at least 32 characters" >&2
    exit 1
    ;;
esac
if [ "${#proxy_auth}" -lt 32 ]; then
  echo "Vonk Forge Caddy proxy authentication secret must be one base64url-like token of at least 32 characters" >&2
  exit 1
fi
export VONK_AGENT_PROXY_AUTH="$proxy_auth"
if [ "$#" -eq 0 ]; then
  set -- caddy run --config /run/vonk-runtime-assets/caddy/Caddyfile --adapter caddyfile
fi
exec "$@"

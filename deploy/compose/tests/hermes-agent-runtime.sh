#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
compose_root="$(cd -- "${script_dir}/.." && pwd)"
runtime_root="$(mktemp -d /tmp/vonk-hermes-agent.XXXXXX)"
project="vonk-hermes-runtime-${RANDOM}-$$"
api_key_file="${runtime_root}/hermes-api-key"

cleanup() {
    docker compose --project-name "${project}" --env-file "${compose_root}/tests/test.env" \
        -f "${compose_root}/compose.yaml" \
        down --volumes --remove-orphans >/dev/null 2>&1 || true
    rm -rf -- "${runtime_root}"
}
trap cleanup EXIT

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

printf '%s\n' 'runtime-test-key-0000000000000000' >"${api_key_file}"
chmod 600 "${api_key_file}"
export HERMES_API_KEY_FILE="${api_key_file}"
export HERMES_DASHBOARD_ORIGIN="https://hermes.runtime.invalid"
docker build \
    --file "${compose_root}/hermes-agent/Dockerfile" \
    --tag local/hermes-agent:managed \
    "${compose_root}/hermes-agent"
export HERMES_AGENT_IMAGE=local/hermes-agent:managed

compose=(
    docker compose --project-name "${project}"
    --env-file "${compose_root}/tests/test.env"
    -f "${compose_root}/compose.yaml"
)

docker run --rm --entrypoint chown \
    --mount "type=bind,source=${api_key_file},target=/run-key" \
    local/hermes-agent:managed 0:0 /run-key
"${compose[@]}" up -d --no-deps hermes-agent
container_id="$("${compose[@]}" ps -q hermes-agent)"
[ -n "${container_id}" ] || fail "Hermes container did not start"

for _ in {1..90}; do
    status="$(docker inspect --format '{{.State.Health.Status}}' "${container_id}" 2>/dev/null || true)"
    [ "${status}" = healthy ] && break
    state="$(docker inspect --format '{{.State.Status}}' "${container_id}" 2>/dev/null || true)"
    [ "${state}" = restarting ] && break
    sleep 1
done
if [ "${status:-}" != healthy ]; then
    docker inspect --format '{{json .State}}' "${container_id}" >&2 || true
    docker logs "${container_id}" >&2 || true
    fail "Hermes did not become healthy on 8642 and 9119"
fi

check_api_auth() {
    mode="$1"
    expected="$2"
    docker exec -i "${container_id}" python - "${mode}" "${expected}" <<'PY'
import json
import pathlib
import sys
import urllib.error
import urllib.request

mode, expected_text = sys.argv[1:]
headers = {}
if mode == "valid":
    key = pathlib.Path("/run/secrets/hermes-api-key").read_text().strip()
    headers["Authorization"] = f"Bearer {key}"
elif mode == "invalid":
    headers["Authorization"] = "Bearer invalid-runtime-test-key"

request = urllib.request.Request("http://127.0.0.1:8642/v1/models", headers=headers)
try:
    with urllib.request.urlopen(request, timeout=3) as response:
        status = response.status
except urllib.error.HTTPError as error:
    status = error.code

expected = int(expected_text)
if status != expected:
    raise SystemExit(f"expected API status {expected}, got {status}")

if mode == "valid":
    detailed = urllib.request.Request(
        "http://127.0.0.1:8642/health/detailed",
        headers=headers,
    )
    with urllib.request.urlopen(detailed, timeout=3) as response:
        pid = json.load(response)["pid"]
    process_status = pathlib.Path(f"/proc/{pid}/status").read_text().splitlines()
    uid = next(line for line in process_status if line.startswith("Uid:"))
    gid = next(line for line in process_status if line.startswith("Gid:"))
    cap_eff = next(line for line in process_status if line.startswith("CapEff:"))
    no_new_privs = next(
        line for line in process_status if line.startswith("NoNewPrivs:")
    )
    if set(uid.split()[1:]) != {"1100"} or set(gid.split()[1:]) != {"1100"}:
        raise SystemExit(f"API process identity mismatch: {uid}; {gid}")
    if int(cap_eff.split()[1], 16) != 0 or no_new_privs.split()[1] != "1":
        raise SystemExit(f"API process privilege mismatch: {cap_eff}; {no_new_privs}")
PY
}

check_api_auth absent 401
check_api_auth invalid 401
check_api_auth valid 200

host_config="$(docker inspect --format '{{json .HostConfig}}' "${container_id}")"
jq -e '.ReadonlyRootfs == true and .Privileged == false and (.CapDrop == ["ALL"]) and ((.CapAdd // []) | sort) == ["CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_SETGID", "CAP_SETUID"] and ((.Devices // []) | length == 0)' \
    <<<"${host_config}" >/dev/null || fail "Hermes container privilege contract failed"
grep -Fq 'docker.sock' <<<"${host_config}" && fail "docker.sock is mounted"
docker exec "${container_id}" sh -c \
    'test -s /run/secrets/hermes-api-key && touch /workspace/runtime-persistent && touch /opt/data/runtime-persistent'
if docker exec "${container_id}" sh -c 'touch /etc/must-remain-read-only' 2>/dev/null; then
    fail "read-only root was writable"
fi

networks="$(docker inspect --format '{{json .NetworkSettings.Networks}}' "${container_id}")"
jq -e '(keys | sort) == (["'"${project}"'_hermes-inference", "'"${project}"'_tailnet-hermes-edge"] | sort)' \
    <<<"${networks}" >/dev/null || fail "Hermes joined an unexpected network"

"${compose[@]}" up -d --no-deps --force-recreate hermes-agent
container_id="$("${compose[@]}" ps -q hermes-agent)"
docker exec "${container_id}" test -f /workspace/runtime-persistent
docker exec "${container_id}" test -f /opt/data/runtime-persistent
docker exec "${container_id}" test -s /opt/data/config.yaml

printf '%s\n' "Hermes Agent runtime isolation and persistence checks passed."

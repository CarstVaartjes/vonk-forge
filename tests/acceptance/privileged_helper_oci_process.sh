#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
  echo 'run this bounded helper proof as root' >&2
  exit 2
fi

helper_binary=${VONK_HELPER_BINARY:?VONK_HELPER_BINARY is required}
probe_binary=${VONK_HELPER_PROBE_BINARY:?VONK_HELPER_PROBE_BINARY is required}
fixture=${VONK_HELPER_FIXTURE:?VONK_HELPER_FIXTURE is required}
report_root=${VONK_HELPER_REPORT_ROOT:-"$PWD/helper-process-proof"}
mkdir -p "$report_root"
chmod 0700 "$report_root"

registry_name=vonk-helper-proof-registry
image_base=localhost:5001/vonk/helper-tiny
image_name="$image_base:v1"
image_platform="$image_base:arm64"
image_ref=''
helper_pid=''
fixture_dir=$(mktemp -d)
archive_sha=''
archive_bytes=''

cleanup() {
  if [[ -n "$helper_pid" ]] && kill -0 "$helper_pid" 2>/dev/null; then
    kill "$helper_pid" 2>/dev/null || true
    wait "$helper_pid" 2>/dev/null || true
  fi
  docker rm --force vonk-proof-run "$registry_name" >/dev/null 2>&1 || true
  rm -rf "$fixture_dir"
}
trap cleanup EXIT

# The native ARM64 runner owns these disposable paths; no laptop socket or host bind is used.
getent group vonk-agent >/dev/null || groupadd --system --gid 10001 vonk-agent
getent passwd vonk-agent >/dev/null || useradd --system --uid 10001 --gid vonk-agent --no-create-home --shell /usr/sbin/nologin vonk-agent
install -d -o root -g root -m 0755 /var/lib/vonk-forge
install -d -o root -g root -m 0700 /var/lib/vonk-forge/helper /var/lib/vonk-forge/helper/requests
install -d -o root -g root -m 0700 /var/lib/vonk-forge/oci-archives
install -d -o root -g root -m 0755 /run/vonk-forge-agent /run/vonk-forge-package-helper
install -d -o vonk-agent -g vonk-agent -m 0700 /var/lib/vonk-forge-agent /run/vonk-forge-agent/runtime-requests
install -d -o vonk-agent -g vonk-agent -m 0700 /var/lib/vonk-forge/models
runtime_probe=/run/vonk-forge-agent/privileged_oci_process_probe
runtime_fixture=/run/vonk-forge-agent/compiled_workload_v2.json
install -o root -g vonk-agent -m 0750 "$probe_binary" "$runtime_probe"
install -o root -g vonk-agent -m 0640 "$fixture" "$runtime_fixture"
probe_binary=$runtime_probe
fixture=$runtime_fixture

cat > "$fixture_dir/Dockerfile" <<'DOCKERFILE'
FROM --platform=linux/arm64 busybox:1.36.1
RUN addgroup -g 10001 vonk \
    && adduser -D -H -u 10001 -G vonk vonk \
    && mkdir -p /outputs/cache/home /outputs/tmp \
    && chown -R 10001:10001 /outputs
USER 10001:10001
ENV HOME=/outputs/cache/home XDG_CACHE_HOME=/outputs/cache TMPDIR=/outputs/tmp
LABEL ai.vonkforge.runtime-interface=v1
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["true"]
DOCKERFILE

docker build --platform linux/arm64 -t "$image_platform" "$fixture_dir" >"$report_root/image-build.log"
docker run --detach --rm --name "$registry_name" -p 5001:5000 registry:2 >"$report_root/registry-id"
for _ in {1..30}; do
  docker push "$image_platform" >"$report_root/image-push.log" 2>&1 && break
  sleep 1
done
for _ in {1..30}; do
  docker manifest create --insecure "$image_name" "$image_platform" >"$report_root/manifest-create.log" 2>&1 && break
  sleep 1
done
docker manifest annotate "$image_name" "$image_platform" --os linux --arch arm64 >"$report_root/manifest-annotate.log"
for _ in {1..30}; do
  docker manifest push --insecure "$image_name" >"$report_root/manifest-push.log" 2>&1 && break
  sleep 1
done
platform_ref=$(docker image inspect "$image_platform" --format '{{index .RepoDigests 0}}')
platform_digest=${platform_ref##*@}
registry_digest=$(curl --fail --silent --show-error --head \
  -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
  "http://localhost:5001/v2/vonk/helper-tiny/manifests/v1" \
  | awk -F': ' 'tolower($1) == "docker-content-digest" {gsub("\r", "", $2); print $2; exit}')
test -n "$registry_digest"
test "$registry_digest" != "$platform_digest"
image_ref="$image_platform@$platform_digest"
config_id=$(docker image inspect "$image_platform" --format '{{.Id}}')
docker image inspect "$image_ref" >"$report_root/source-image-ref-inspect.json"
docker save --output "$fixture_dir/image.oci.tar" "$image_ref"
archive_sha=$(sha256sum "$fixture_dir/image.oci.tar" | awk '{print $1}')
archive_bytes=$(stat -c '%s' "$fixture_dir/image.oci.tar")
cp "$fixture_dir/image.oci.tar" "/var/lib/vonk-forge/oci-archives/$archive_sha"
chown vonk-agent:vonk-agent "/var/lib/vonk-forge/oci-archives/$archive_sha"
chmod 0600 "/var/lib/vonk-forge/oci-archives/$archive_sha"

install -d -o vonk-agent -g vonk-agent -m 0700 \
  /var/lib/vonk-forge/models/primary \
  /var/lib/vonk-forge/models/dependency-qwen3-8-27b-dspark-b3c99101 \
  /var/lib/vonk-forge/models/support \
  /var/lib/vonk-forge-agent/runs/proof-run/outputs/tmp/proof-run \
  /var/lib/vonk-forge-agent/run-metadata/proof-run \
  /var/lib/vonk-forge-agent/installations/proof-install/runtime-cache
for path in \
  /var/lib/vonk-forge/models/primary/config.json \
  /var/lib/vonk-forge/models/dependency-qwen3-8-27b-dspark-b3c99101/config.json \
  /var/lib/vonk-forge/models/support/__init__.py; do
  printf 'helper-process-proof\n' >"$path"
  chown vonk-agent:vonk-agent "$path"
  chmod 0600 "$path"
done
printf '{}\n' >/var/lib/vonk-forge-agent/run-metadata/proof-run/runtime.json
chown vonk-agent:vonk-agent /var/lib/vonk-forge-agent/run-metadata/proof-run/runtime.json
chmod 0600 /var/lib/vonk-forge-agent/run-metadata/proof-run/runtime.json

export VONK_HELPER_ARCHIVE_SHA="$archive_sha"
export VONK_HELPER_ARCHIVE_BYTES="$archive_bytes"
export VONK_HELPER_REGISTRY_DIGEST="$registry_digest"
export VONK_HELPER_PLATFORM_DIGEST="$platform_digest"
export VONK_HELPER_CONFIG_ID="$config_id"
export VONK_HELPER_IMAGE_REF="$image_ref"
export VONK_HELPER_FIXTURE="$fixture"
export VONK_HELPER_SOCKET=/run/vonk-forge-package-helper/package-helper.sock
export VONK_HELPER_REQUEST_ROOT=/run/vonk-forge-agent/runtime-requests

"$probe_binary" setup >"$report_root/setup.log"
chown root:vonk-agent /etc/vonk-forge-agent/observation-receipt.pub
chmod 0640 /etc/vonk-forge-agent/observation-receipt.pub
for ref in "$image_ref" "$image_platform" "$image_name"; do
  docker image rm "$ref" >>"$report_root/source-image-removal.log" 2>&1 || true
done
if docker image inspect "$image_ref" >/dev/null 2>&1; then
  echo 'source image remained installed before helper import' >&2
  exit 1
fi
rm -f /run/vonk-forge-package-helper/package-helper.sock
systemd-socket-activate \
  --listen=/run/vonk-forge-package-helper/package-helper.sock \
  --fdname=helper \
  "$helper_binary" >"$report_root/helper.log" 2>&1 &
helper_pid=$!
for _ in {1..30}; do
  [[ -S /run/vonk-forge-package-helper/package-helper.sock ]] && break
  sleep 1
done
[[ -S /run/vonk-forge-package-helper/package-helper.sock ]]
chown root:vonk-agent /run/vonk-forge-package-helper/package-helper.sock
chmod 0660 /run/vonk-forge-package-helper/package-helper.sock

sudo -u vonk-agent -g vonk-agent env \
  VONK_HELPER_ARCHIVE_SHA="$archive_sha" \
  VONK_HELPER_ARCHIVE_BYTES="$archive_bytes" \
  VONK_HELPER_REGISTRY_DIGEST="$VONK_HELPER_REGISTRY_DIGEST" \
  VONK_HELPER_PLATFORM_DIGEST="$platform_digest" \
  VONK_HELPER_CONFIG_ID="$config_id" \
  VONK_HELPER_IMAGE_REF="$image_ref" \
  VONK_HELPER_FIXTURE="$fixture" \
  VONK_HELPER_SOCKET="$VONK_HELPER_SOCKET" \
  VONK_HELPER_REQUEST_ROOT="$VONK_HELPER_REQUEST_ROOT" \
  "$probe_binary" import | tee "$report_root/import.log"

sudo -u vonk-agent -g vonk-agent env \
  VONK_HELPER_ARCHIVE_SHA="$archive_sha" \
  VONK_HELPER_ARCHIVE_BYTES="$archive_bytes" \
  VONK_HELPER_REGISTRY_DIGEST="$VONK_HELPER_REGISTRY_DIGEST" \
  VONK_HELPER_PLATFORM_DIGEST="$platform_digest" \
  VONK_HELPER_CONFIG_ID="$config_id" \
  VONK_HELPER_IMAGE_REF="$image_ref" \
  VONK_HELPER_FIXTURE="$fixture" \
  VONK_HELPER_SOCKET="$VONK_HELPER_SOCKET" \
  VONK_HELPER_REQUEST_ROOT="$VONK_HELPER_REQUEST_ROOT" \
  "$probe_binary" start | tee "$report_root/start.log"

sleep 6
docker logs vonk-proof-run >"$report_root/container-first.log" 2>&1
grep -q 'uid=10001' "$report_root/container-first.log"
grep -q 'cache-created' "$report_root/container-first.log"
grep -q 'tmp-fresh' "$report_root/container-first.log"
test "$(cat /var/lib/vonk-forge-agent/installations/proof-install/runtime-cache/helper-cache-ok)" = cache-created
docker rm vonk-proof-run >"$report_root/container-first-remove.log"
sudo -u vonk-agent -g vonk-agent env \
  VONK_HELPER_ARCHIVE_SHA="$archive_sha" \
  VONK_HELPER_ARCHIVE_BYTES="$archive_bytes" \
  VONK_HELPER_REGISTRY_DIGEST="$VONK_HELPER_REGISTRY_DIGEST" \
  VONK_HELPER_PLATFORM_DIGEST="$platform_digest" \
  VONK_HELPER_CONFIG_ID="$config_id" \
  VONK_HELPER_IMAGE_REF="$image_ref" \
  VONK_HELPER_FIXTURE="$fixture" \
  VONK_HELPER_SOCKET="$VONK_HELPER_SOCKET" \
  VONK_HELPER_REQUEST_ROOT="$VONK_HELPER_REQUEST_ROOT" \
  "$probe_binary" start | tee "$report_root/start-reuse.log"

docker inspect vonk-proof-run >"$report_root/container-inspect.json"
sleep 6
docker logs vonk-proof-run >"$report_root/container.log" 2>&1 || true

grep -q '"--network","none"' "$report_root/start.log"
grep -q '"--tmpfs"' "$report_root/start.log"
grep -q '"--read-only"' "$report_root/start.log"
grep -q '"--cap-drop=ALL"' "$report_root/start.log"
grep -q '"--security-opt=no-new-privileges"' "$report_root/start.log"
grep -q '"--user","10001:10001"' "$report_root/start.log"
grep -q '"--entrypoint","/bin/sh"' "$report_root/start.log"
if grep -q '"--device"' "$report_root/start.log"; then
  echo 'unexpected device flag in the GPU-free proof' >&2
  exit 1
fi
grep -q 'uid=10001' "$report_root/container.log"
grep -q 'helper-argv-once' "$report_root/container.log"
grep -q 'cache-reused' "$report_root/container.log"
grep -q 'tmp-fresh' "$report_root/container.log"
test -f /var/lib/vonk-forge-agent/installations/proof-install/runtime-cache/home/helper-entrypoint-ok
test "$(cat /var/lib/vonk-forge-agent/installations/proof-install/runtime-cache/helper-cache-ok)" = cache-created
test -f /var/lib/vonk-forge-agent/runs/proof-run/outputs/tmp/proof-run/helper-tmp-ok
printf 'helper-process-proof=passed\narchive_sha256=%s\narchive_bytes=%s\nimage_ref=%s\nplatform_digest=%s\nconfig_id=%s\n' \
  "$archive_sha" "$archive_bytes" "$image_ref" "$platform_digest" "$config_id" | tee "$report_root/summary.txt"

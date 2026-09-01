#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
  printf '%s\n' 'agent upgrade recovery test must run as root' >&2
  exit 77
fi
if [[ ! -d /run/systemd/system ]]; then
  printf '%s\n' 'agent upgrade recovery test requires systemd' >&2
  exit 77
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
version=${VERSION:-0.1.0}
semantic_version=${version%%~*}
semantic_version=${semantic_version%%+*}
baseline_version=0.0.0~acceptance.1+g0123456789ab
baseline_semantic=0.0.0
stale_pending_format=${STALE_PENDING_FORMAT:-prior3}
crash_mode=${CRASH_MODE:-full-cgroup}
candidate_custody=${CANDIDATE_CUSTODY:-legacy}
compatibility_trigger=0
if [[ "$stale_pending_format" == legacy2 \
  && "$crash_mode" == full-cgroup \
  && "$candidate_custody" == legacy ]]; then
  compatibility_trigger=1
fi
case "$crash_mode" in
  full-cgroup|dpkg-only|post-remove) ;;
  *) printf 'unknown crash mode fixture: %s\n' "$crash_mode" >&2; exit 64 ;;
esac
case "$candidate_custody" in
  legacy|root) ;;
  *) printf 'unknown candidate custody fixture: %s\n' "$candidate_custody" >&2; exit 64 ;;
esac
case "$(dpkg --print-architecture)" in
  amd64) build_arch=linux-amd64 ;;
  arm64) build_arch=linux-arm64 ;;
  *) printf '%s\n' 'unsupported recovery test architecture' >&2; exit 77 ;;
esac

if (( compatibility_trigger == 1 )); then
  # Keep the Controller's durable replay window at least twice the largest
  # polling backoff accepted by the packaged agent. This systemd lane is the
  # physical lifecycle gate for the exact dev335 -> a122 compatibility path.
  python3 - \
    "$repo_root/control/src/vonk_control/compat_recovery.py" \
    "$repo_root/control/src/vonk_control/agent_jobs.py" \
    "$repo_root/rust/crates/vonk-agent/src/config.rs" <<'PY'
import pathlib
import re
import sys

compatibility = pathlib.Path(sys.argv[1]).read_text()
jobs = pathlib.Path(sys.argv[2]).read_text()
agent_config = pathlib.Path(sys.argv[3]).read_text()
rearm_minutes = int(
    re.search(r"_REARM_LEASE = timedelta\(minutes=(\d+)\)", compatibility).group(1)
)
arm_minutes = int(
    re.search(
        r"_COMPATIBILITY_ARM_TIMEOUT = timedelta\(minutes=(\d+)\)", jobs
    ).group(1)
)
max_backoff = int(
    re.search(r"poll_max_seconds > (\d+)", agent_config).group(1)
)
assert rearm_minutes == arm_minutes
assert rearm_minutes * 60 >= max_backoff * 2
PY
fi

test_root="$(mktemp -d /var/lib/vonk-forge-recovery-test.XXXXXX)"
old_helper_unit=/lib/systemd/system/vonk-forge-package-helper.service
old_socket_unit=/lib/systemd/system/vonk-forge-package-helper.socket
helper_unit=vonk-forge-package-helper.service
socket_unit=vonk-forge-package-helper.socket
agent_unit=vonk-forge-agent.service
recovery_unit=vonk-forge-package-upgrade-recover-capsule.service
package_recovery_unit=vonk-forge-package-upgrade-recover.service
recovery_unit_path=/lib/systemd/system/$recovery_unit
recovery_enablement=/etc/systemd/system/multi-user.target.wants/$recovery_unit
recovery_gate=/lib/systemd/system/vonk-forge-agent.service.d/10-package-upgrade-capsule.conf
recovery_suppression=/lib/systemd/system/$package_recovery_unit.d/10-capsule-owner.conf
agent_unit_path=/lib/systemd/system/$agent_unit
agent_enablement=/lib/systemd/system/multi-user.target.wants/$agent_unit
firewall_unit=vonk-forge-docker-firewall.service
firewall_fixture=/run/systemd/system/$firewall_unit
started=$test_root/start
crash_observed=$test_root/crash-observed
# The simulated old helper can write only its declared ReadWritePaths.
upgrade_invocations=/var/lib/vonk-forge/upgrade-invocations.$(basename "$test_root")
compat_request_ledger=/var/lib/vonk-forge/helper/requests/35420000-0000-4000-8000-000000000001
compat_collision_ledger=/var/lib/vonk-forge/helper/requests/35420000-0000-4000-8000-000000000002
compat_reboot_service=vonk-forge-reboot.service
compat_reboot_timer=vonk-forge-reboot.timer
compat_agent_config=/etc/vonk-forge-agent/agent.toml
compat_authority_key=/etc/vonk-forge-agent/host-helper-authority.pub
observation_receipt_private=/var/lib/vonk-forge/helper/observation-receipt.pk8
observation_receipt_public=/etc/vonk-forge-agent/observation-receipt.pub

dump_failure_diagnostics() {
  printf '%s\n' '--- agent upgrade recovery fixture diagnostics ---' >&2
  systemctl --system --no-pager --full status \
    "$helper_unit" "$socket_unit" "$recovery_unit" "$package_recovery_unit" \
    "$agent_unit" \
    "$firewall_unit" "$compat_reboot_service" "$compat_reboot_timer" \
    >&2 || true
  journalctl --system --no-pager -n 200 \
    -u "$helper_unit" -u "$socket_unit" -u "$recovery_unit" \
    -u "$package_recovery_unit" -u "$agent_unit" \
    -u "$firewall_unit" -u "$compat_reboot_service" -u "$compat_reboot_timer" \
    >&2 || true
  dpkg-query -W -f='${db:Status-Abbrev} ${Version}\n' vonk-forge-agent \
    >&2 || true
  for state_file in \
    /var/lib/vonk-forge/package-upgrade/intent \
    /var/lib/vonk-forge/package-upgrade.status \
    /var/lib/vonk-forge/helper-upgrade.pending \
    /var/lib/vonk-forge/helper-upgrade.receipt; do
    if [[ -f "$state_file" ]]; then
      printf '%s\n' "--- $state_file ---" >&2
      sed -E 's/^(recovery_nonce)=.*/\1=<redacted>/' "$state_file" >&2 || true
    fi
  done
}

cleanup() {
  cleanup_status=$?
  if (( cleanup_status != 0 )); then
    dump_failure_diagnostics
  fi
  if [[ -n "${dpkg_lock_pid:-}" ]]; then
    touch "$test_root/release-dpkg-lock" 2>/dev/null || true
    kill "$dpkg_lock_pid" >/dev/null 2>&1 || true
    wait "$dpkg_lock_pid" >/dev/null 2>&1 || true
  fi
  systemctl --system thaw "$helper_unit" >/dev/null 2>&1 || true
  systemctl --system stop "$recovery_unit" "$package_recovery_unit" \
    "$agent_unit" "$helper_unit" "$socket_unit" "$firewall_unit" \
    "$compat_reboot_service" \
    "$compat_reboot_timer" \
    >/dev/null 2>&1 || true
  systemctl --system reset-failed "$recovery_unit" "$package_recovery_unit" \
    "$agent_unit" "$helper_unit" "$socket_unit" "$firewall_unit" \
    "$compat_reboot_service" \
    "$compat_reboot_timer" \
    >/dev/null 2>&1 || true
  rm -rf -- /var/lib/vonk-forge/package-upgrade
  rm -f -- /var/lib/vonk-forge/helper-upgrade.pending \
    /var/lib/vonk-forge/helper-upgrade.receipt \
    /var/lib/vonk-forge/package-upgrade.status
  rm -f -- "$compat_request_ledger" "$compat_collision_ledger" \
    "$observation_receipt_private"
  rmdir --ignore-fail-on-non-empty /var/lib/vonk-forge/helper/requests \
    /var/lib/vonk-forge/helper >/dev/null 2>&1 || true
  if dpkg-query --show vonk-forge-agent >/dev/null 2>&1; then
    SYSTEMD_OFFLINE=1 dpkg --purge --force-remove-reinstreq \
      vonk-forge-agent >/dev/null 2>&1 || cleanup_status=1
  fi
  if dpkg-query --show vonk-forge-agent >/dev/null 2>&1; then
    cleanup_status=1
  fi
  rm -rf -- /var/lib/vonk-forge-agent
  rm -f -- "$compat_agent_config" "$compat_authority_key" \
    "$observation_receipt_public"
  rmdir --ignore-fail-on-non-empty /etc/vonk-forge-agent \
    >/dev/null 2>&1 || true
  rm -f -- "$upgrade_invocations"
  rm -f -- "$recovery_enablement" "$recovery_unit_path" \
    "$recovery_gate" "$recovery_suppression" \
    "$agent_enablement" "$agent_unit_path"
  rm -f -- "$old_helper_unit" "$old_socket_unit"
  rm -f -- "$firewall_fixture"
  rm -rf -- /lib/systemd/system/vonk-forge-package-helper.socket.d
  systemctl --system daemon-reload >/dev/null 2>&1 || true
  for _ in {1..100}; do
    reboot_service_load=$(systemctl --system show --property=LoadState --value \
      "$compat_reboot_service" 2>/dev/null || true)
    reboot_timer_load=$(systemctl --system show --property=LoadState --value \
      "$compat_reboot_timer" 2>/dev/null || true)
    [[ "$reboot_service_load" == not-found \
      && "$reboot_timer_load" == not-found ]] && break
    sleep 0.05
  done
  rm -rf -- /run/vonk-forge-package-candidates
  rm -rf -- "$test_root"
  trap - EXIT
  exit "$cleanup_status"
}
cleanup_test_root() {
  rm -rf -- "$test_root"
}
trap cleanup_test_root EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if dpkg-query -W vonk-forge-agent >/dev/null 2>&1 \
  || systemctl --system cat "$agent_unit" >/dev/null 2>&1 \
  || systemctl --system cat "$helper_unit" >/dev/null 2>&1 \
  || systemctl --system cat "$socket_unit" >/dev/null 2>&1 \
  || systemctl --system cat "$recovery_unit" >/dev/null 2>&1 \
  || systemctl --system cat "$package_recovery_unit" >/dev/null 2>&1 \
  || [[ -e "$recovery_gate" || -L "$recovery_gate" \
    || -e "$recovery_suppression" || -L "$recovery_suppression" \
    || -e "$recovery_enablement" || -L "$recovery_enablement" ]] \
  || systemctl --system cat "$firewall_unit" >/dev/null 2>&1 \
  || [[ -e /var/lib/vonk-forge/package-upgrade \
    || -L /var/lib/vonk-forge/package-upgrade ]] \
  || [[ -e /var/lib/vonk-forge/helper-upgrade.pending \
    || -L /var/lib/vonk-forge/helper-upgrade.pending ]] \
  || [[ -e /var/lib/vonk-forge/helper-upgrade.receipt \
    || -L /var/lib/vonk-forge/helper-upgrade.receipt ]] \
  || [[ -e /var/lib/vonk-forge/package-upgrade.status \
    || -L /var/lib/vonk-forge/package-upgrade.status ]] \
  || [[ -e "$compat_request_ledger" || -L "$compat_request_ledger" \
    || -e "$compat_collision_ledger" || -L "$compat_collision_ledger" ]] \
  || [[ -e "$observation_receipt_private" \
    || -L "$observation_receipt_private" ]] \
  || [[ -e /etc/vonk-forge-agent || -L /etc/vonk-forge-agent ]] \
  || [[ "$(systemctl --system show --property=LoadState --value \
    "$compat_reboot_service" 2>/dev/null || true)" != not-found ]] \
  || [[ "$(systemctl --system show --property=LoadState --value \
    "$compat_reboot_timer" 2>/dev/null || true)" != not-found ]] \
  || [[ -e /lib/systemd/system/vonk-forge-agent.service.d/20-package-upgrade-recovery.conf \
    || -L /lib/systemd/system/vonk-forge-agent.service.d/20-package-upgrade-recovery.conf ]] \
  || [[ -e /lib/systemd/system/vonk-forge-package-helper.socket.d \
    || -L /lib/systemd/system/vonk-forge-package-helper.socket.d ]] \
  || [[ -e /run/vonk-forge-package-candidates \
    || -L /run/vonk-forge-package-candidates ]] \
  || [[ -e "$firewall_fixture" || -L "$firewall_fixture" ]] \
  || [[ -e /var/lib/vonk-forge-agent \
    || -L /var/lib/vonk-forge-agent ]]; then
  printf '%s\n' 'agent upgrade recovery fixture would collide with host state' >&2
  exit 1
fi
trap cleanup EXIT

# Setup normally owns this dependency. The package lifecycle fixture uses an
# isolated active unit so it exercises helper recovery without modifying the
# runner's firewall; firewall behavior has its own namespace acceptance test.
cat > "$firewall_fixture" <<'UNIT'
[Unit]
Description=Vonk Forge package recovery firewall fixture

[Service]
Type=oneshot
ExecStart=/bin/true
RemainAfterExit=yes
UNIT
chmod 0644 "$firewall_fixture"
systemctl --system daemon-reload
systemctl --system start "$firewall_unit"
test "$(systemctl --system show --property=ActiveState --value \
  "$firewall_unit")" = active

mkdir -p "$test_root/target-bin" "$test_root/baseline-bin" \
  "$test_root/target-dist" "$test_root/baseline-dist"
build_digest="sha256:$(printf durable-recovery-fixture | sha256sum | cut -d' ' -f1)"
cat > "$test_root/agent.c" <<SOURCE
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
static volatile const char build[] = "VONK_AGENT_BUILD_DIGEST=$build_digest";
static volatile const char semantic[] = "VONK_AGENT_SEMANTIC_VERSION=$semantic_version";
int main(int argc, char **argv) {
  if (argc == 2 && strcmp(argv[1], "--version") == 0) {
    printf("vonk-agent %s\\n", "$semantic_version");
    return build[0] == 'V' && semantic[0] == 'V' ? 0 : 1;
  }
  for (;;) pause();
}
SOURCE
cat > "$test_root/helper.c" <<'SOURCE'
#include <signal.h>
#include <unistd.h>
int main(void) { for (;;) pause(); }
SOURCE
gcc -O2 -o "$test_root/target-bin/vonk-agent" "$test_root/agent.c"
cat > "$test_root/baseline-agent.c" <<SOURCE
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
static volatile const char build[] = "VONK_AGENT_BUILD_DIGEST=$build_digest";
static volatile const char semantic[] = "VONK_AGENT_SEMANTIC_VERSION=$baseline_semantic";
int main(int argc, char **argv) {
  if (argc == 2 && strcmp(argv[1], "--version") == 0) {
    printf("vonk-agent %s\\n", "$baseline_semantic");
    return build[0] == 'V' && semantic[0] == 'V' ? 0 : 1;
  }
  for (;;) pause();
}
SOURCE
gcc -O2 -o "$test_root/baseline-bin/vonk-agent" \
  "$test_root/baseline-agent.c"
gcc -O2 -o "$test_root/target-bin/vonk-agent-helper" "$test_root/helper.c"
if [[ -n "${BUILD_EGRESS_BINARY:-}" ]]; then
  build_egress_fixture=$(realpath -e -- "$BUILD_EGRESS_BINARY")
  test "$build_egress_fixture" = \
    "$repo_root/target/release/vonk-build-egress"
else
  RUSTFLAGS='-C target-feature=+crt-static' \
    cargo build --locked --release --manifest-path "$repo_root/Cargo.toml" \
      --package vonk-build-egress
  build_egress_fixture=$repo_root/target/release/vonk-build-egress
fi
test ! -L "$build_egress_fixture"
test -f "$build_egress_fixture"
test -x "$build_egress_fixture"
if (( compatibility_trigger == 1 )); then
  dev335_source=$test_root/dev335-source
  mkdir -p "$dev335_source"
  git -C "$repo_root" archive \
    2eaaf4d9b2b56b7090541873b5700e04ad9e3662 \
    | tar -x -C "$dev335_source"
  CARGO_TARGET_DIR=$test_root/dev335-target cargo build \
    --locked --release --manifest-path "$dev335_source/Cargo.toml" \
    -p vonk-agent-helper
  cp -- "$test_root/dev335-target/release/vonk-agent-helper" \
    "$test_root/baseline-bin/vonk-agent-helper"
else
  cp -- "$test_root/target-bin/vonk-agent-helper" \
    "$test_root/baseline-bin/vonk-agent-helper"
fi
for binary_dir in "$test_root/target-bin" "$test_root/baseline-bin"; do
  cp -- "$binary_dir/vonk-agent-helper" "$binary_dir/oras"
  cp -- "$build_egress_fixture" "$binary_dir/vonk-build-egress"
  printf '%s\n' 'ORAS recovery fixture license' > "$binary_dir/oras.LICENSE"
  chmod 0555 "$binary_dir/vonk-agent" "$binary_dir/vonk-agent-helper" \
    "$binary_dir/vonk-build-egress" "$binary_dir/oras"
done
openssl genpkey -algorithm ED25519 -out "$test_root/release.pem"
chmod 0600 "$test_root/release.pem"
epoch=$(git -C "$repo_root" show -s --format=%ct HEAD)
VONK_SOURCE_REVISION=$(git -C "$repo_root" rev-parse HEAD) \
VONK_SOURCE_REPOSITORY=https://github.com/CarstVaartjes/vonk-forge \
  "$repo_root/scripts/build-agent-deb" \
    --version "$version" \
    --architecture "$build_arch" \
    --build-digest "$build_digest" \
    --release-private-key "$test_root/release.pem" \
    --binaries-dir "$test_root/target-bin" \
    --source-date-epoch "$epoch" \
    --output-dir "$test_root/target-dist"

VONK_SOURCE_REVISION=$(git -C "$repo_root" rev-parse HEAD) \
VONK_SOURCE_REPOSITORY=https://github.com/CarstVaartjes/vonk-forge \
  "$repo_root/scripts/build-agent-deb" \
    --version "$baseline_version" \
    --architecture "$build_arch" \
    --build-digest "$build_digest" \
    --release-private-key "$test_root/release.pem" \
    --binaries-dir "$test_root/baseline-bin" \
    --source-date-epoch "$epoch" \
    --output-dir "$test_root/baseline-dist" \
    --acceptance-baseline

package=$(find "$test_root/target-dist" -maxdepth 1 -type f -name '*.deb')
baseline_package=$(find "$test_root/baseline-dist" -maxdepth 1 -type f \
  -name '*.deb')
if [[ "$crash_mode" == post-remove ]]; then
  # Make the first recovery install fail in postinst, then hold the fallback
  # install's preinst after dpkg has completely removed the package. These
  # wrappers exist only in the synthetic native fixture; the embedded recovery
  # runner and its bound capsule are otherwise the exact production scripts.
  fault_package_root=$test_root/fault-package
  dpkg-deb --raw-extract "$package" "$fault_package_root"
  mv -- "$fault_package_root/DEBIAN/preinst" \
    "$fault_package_root/DEBIAN/preinst.production"
  {
    cat <<'PREINST_FAULT'
#!/bin/sh
set -eu
fault_root=/var/lib/vonk-forge/package-upgrade
if [ -n "${VONK_FORGE_UPGRADE_RECOVERY_NONCE:-}" ] \
    && [ -f "$fault_root/test-force-postinst-failure" ] \
    && [ ! -e "$fault_root/test-post-remove-preinst-entered" ]; then
    /usr/bin/touch "$fault_root/test-post-remove-preinst-entered"
    /usr/bin/sync -f "$fault_root/test-post-remove-preinst-entered"
    /usr/bin/sync -f "$fault_root"
    while :; do /bin/sleep 1; done
fi
PREINST_FAULT
    cat "$fault_package_root/DEBIAN/preinst.production"
  } > "$fault_package_root/DEBIAN/preinst"
  chmod 0755 "$fault_package_root/DEBIAN/preinst"
  rm -- "$fault_package_root/DEBIAN/preinst.production"
  install -o root -g root -m 0555 "$fault_package_root/DEBIAN/preinst" \
    "$fault_package_root/usr/lib/vonk-forge/vonk-forge-package-upgrade-recover"
  mv -- "$fault_package_root/DEBIAN/postinst" \
    "$fault_package_root/DEBIAN/postinst.production"
  {
    cat <<'POSTINST_FAULT'
#!/bin/sh
set -eu
fault_root=/var/lib/vonk-forge/package-upgrade
if [ -n "${VONK_FORGE_UPGRADE_RECOVERY_NONCE:-}" ] \
    && [ ! -e "$fault_root/test-force-postinst-failure" ]; then
    /usr/bin/touch "$fault_root/test-force-postinst-failure"
    /usr/bin/sync -f "$fault_root/test-force-postinst-failure"
    /usr/bin/sync -f "$fault_root"
    exit 1
fi
POSTINST_FAULT
    cat "$fault_package_root/DEBIAN/postinst.production"
  } > "$fault_package_root/DEBIAN/postinst"
  chmod 0755 "$fault_package_root/DEBIAN/postinst"
  rm -- "$fault_package_root/DEBIAN/postinst.production"
  dpkg-deb --build --root-owner-group "$fault_package_root" "$package"
fi
package_digest=$(sha256sum "$package" | cut -d' ' -f1)
target_recovery_runner=$test_root/target-package-upgrade-recover
dpkg-deb --fsys-tarfile "$package" \
  | tar -xOf - ./usr/lib/vonk-forge/vonk-forge-package-upgrade-recover \
  > "$target_recovery_runner"
chmod 0555 "$target_recovery_runner"
target_recovery_runner_digest=$(sha256sum "$target_recovery_runner" \
  | cut -d' ' -f1)
agent_digest=$(sha256sum "$test_root/target-bin/vonk-agent" | cut -d' ' -f1)
helper_digest=$(sha256sum "$test_root/target-bin/vonk-agent-helper" | cut -d' ' -f1)
baseline_agent_digest=$(sha256sum "$test_root/baseline-bin/vonk-agent" \
  | cut -d' ' -f1)
if (( compatibility_trigger == 1 )); then
  dev335_helper_digest=$(sha256sum \
    "$test_root/baseline-bin/vonk-agent-helper" | cut -d' ' -f1)
fi

getent group vonk-agent >/dev/null 2>&1 || groupadd --system vonk-agent
id -u vonk-agent >/dev/null 2>&1 \
  || useradd --system --gid vonk-agent --home-dir /var/lib/vonk-forge-agent \
    --shell /usr/sbin/nologin vonk-agent
install -d -o vonk-agent -g vonk-agent -m 0700 /var/lib/vonk-forge-agent
install -d -o root -g root -m 0755 /var/lib/vonk-forge
install -d -o vonk-agent -g vonk-agent -m 0700 /var/lib/vonk-forge/incoming
case "$candidate_custody" in
  legacy)
    candidate=/var/lib/vonk-forge/incoming/$package_digest.deb
    helper_runtime_directory=vonk-forge-package-helper
    helper_runtime_mode=0711
    helper_runtime_preserve=yes
    ;;
  root)
    custody_root=/run/vonk-forge-package-candidates
    custody_invocation=0123456789abcdef0123456789abcdef
    candidate=$custody_root/$custody_invocation/$package_digest.deb
    helper_runtime_directory=vonk-forge-package-candidates
    helper_runtime_mode=0700
    helper_runtime_preserve=restart
    ;;
esac

# Install the synthetic lower version before creating the old helper mount
# namespace. This makes every historical ReadWritePaths target exist on a clean
# runner. Preserve the newly unpacked unit files so they can be restored on disk
# after systemd has started the exact old helper sandbox, matching the live
# state where the old process survives but the package payload is already new.
SYSTEMD_OFFLINE=1 dpkg --unpack --force-confold "$baseline_package"
test "$(dpkg-query -W -f='${db:Status-Abbrev}' vonk-forge-agent | cut -c1-2)" = iU
test "$(dpkg-query -W -f='${Version}' vonk-forge-agent)" = "$baseline_version"
cp -- "$old_helper_unit" "$test_root/installed-helper.service"
cp -- "$old_socket_unit" "$test_root/installed-helper.socket"

assert_interrupted_baseline_state() {
  interrupted_state=$(dpkg-query -W \
    -f='${db:Status-Abbrev}|${Version}' vonk-forge-agent)
  case "$interrupted_state" in
    "iU |$baseline_version"|"iHR|$baseline_version") ;;
    *)
      printf 'unexpected interrupted package state: %s\n' \
        "$interrupted_state" >&2
      return 1
      ;;
  esac
}

stage_candidate() {
  case "$candidate_custody" in
    legacy)
      install -o vonk-agent -g vonk-agent -m 0600 "$package" "$candidate"
      test "$(stat -c %U:%G:%a:%h "$candidate")" = vonk-agent:vonk-agent:600:1
      ;;
    root)
      test "$(stat -c %u:%g:%a "$custody_root")" = 0:0:700
      install -d -o root -g root -m 0700 "$custody_root/$custody_invocation"
      install -o root -g root -m 0600 "$package" "$candidate"
      test "$(stat -c %u:%g:%a "$custody_root/$custody_invocation")" = 0:0:700
      test "$(stat -c %u:%g:%a:%h "$candidate")" = 0:0:600:1
      test "$candidate" \
        = "/run/vonk-forge-package-candidates/$custody_invocation/$package_digest.deb"
      ;;
  esac
  test "$(basename "$candidate")" = "$package_digest.deb"
}

cat > "$test_root/old-helper" <<WRAPPER
#!/usr/bin/env bash
set -euo pipefail
while [[ ! -e "$started" ]]; do sleep 0.05; done
printf '%s\n' invocation >> "$upgrade_invocations"
set +e
/usr/bin/dpkg --install --force-confold "$candidate"
set -e
exec /bin/sleep 300
WRAPPER
chmod 0555 "$test_root/old-helper"
cat > "$old_helper_unit" <<UNIT
[Unit]
Description=Vonk Forge dev335 recovery fixture
Requires=$socket_unit
After=$socket_unit

[Service]
Type=simple
ExecStart=$test_root/old-helper
User=root
Group=root
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
IPAddressDeny=any
ProtectSystem=strict
ProtectHome=yes
ProtectHostname=yes
ProtectClock=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@mount @raw-io @obsolete @debug
RestrictAddressFamilies=AF_UNIX AF_NETLINK
CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER CAP_FSETID CAP_NET_ADMIN CAP_SETGID CAP_SETUID
AmbientCapabilities=
RuntimeDirectory=$helper_runtime_directory
RuntimeDirectoryMode=$helper_runtime_mode
RuntimeDirectoryPreserve=$helper_runtime_preserve
ReadWritePaths=/var/lib/vonk-forge /var/lib/dpkg /var/log /var/cache/apt /var/cache/debconf /etc/vonk-forge-agent /usr/lib/vonk-forge /usr/bin /lib/systemd/system
ReadWritePaths=/usr/share/keyrings /usr/share/doc/vonk-forge-agent
BindReadOnlyPaths=-/run/docker.sock -/run/vonk-forge-agent/runtime-requests -/var/lib/vonk-forge-agent/image-imports
ReadWritePaths=-/var/lib/vonk-forge-agent/models -/var/lib/vonk-forge-agent/runs -/var/lib/vonk-forge-agent/run-metadata
TimeoutStartSec=30s
TimeoutStopSec=15s
KillMode=mixed
UNIT
cat > "$old_socket_unit" <<'UNIT'
[Unit]
Description=Vonk Forge dev335 recovery socket fixture

[Socket]
ListenStream=/run/vonk-forge-package-helper/package-helper.sock
SocketMode=0660
SocketUser=root
SocketGroup=vonk-agent
DirectoryMode=0711
RemoveOnStop=yes
Service=vonk-forge-package-helper.service

[Install]
WantedBy=sockets.target
UNIT
chmod 0644 "$old_helper_unit" "$old_socket_unit"
systemctl --system daemon-reload
systemctl --system enable --now "$socket_unit" >/dev/null
systemctl --system start "$helper_unit"
install -o root -g root -m 0644 "$test_root/installed-helper.service" \
  "$old_helper_unit"
install -o root -g root -m 0644 "$test_root/installed-helper.socket" \
  "$old_socket_unit"
if (( compatibility_trigger == 1 )); then
  systemctl --system start "$agent_unit"
  test "$(systemctl --system show --property=ActiveState --value \
    "$agent_unit")" = active
fi
stage_candidate

case "$stale_pending_format" in
  legacy2)
    printf '%s\n' \
      "version=$baseline_version" \
      'state=pre-unpack' \
      > /var/lib/vonk-forge/helper-upgrade.pending
    ;;
  prior3)
    printf '%s\n' \
      "version=$baseline_version" \
      "helper_sha256=$helper_digest" \
      "agent_sha256=$baseline_agent_digest" \
      > /var/lib/vonk-forge/helper-upgrade.pending
    ;;
  *)
    printf 'unknown stale pending fixture: %s\n' "$stale_pending_format" >&2
    exit 64
    ;;
esac
chown root:root /var/lib/vonk-forge/helper-upgrade.pending
chmod 0600 /var/lib/vonk-forge/helper-upgrade.pending
cp -- /var/lib/vonk-forge/helper-upgrade.pending "$test_root/stale-pending"
printf '%s\n' \
  "version=$version" \
  "helper_sha256=$helper_digest" \
  "agent_sha256=$agent_digest" \
  > "$test_root/normalized-pending"

# Kill the complete old-helper cgroup after atomic intent rename. The watcher
# can freeze a known durability sync before it returns; the post-freeze process
# classifier below proves that exact safe case. The target preinst is allowed to
# normalize the safe stale gate before the watcher runs, so capture and prove
# whichever exact safe state exists at the crash point.
(
  for _ in {1..2400}; do
    if [[ -f /var/lib/vonk-forge/package-upgrade/intent ]]; then
      systemctl --system freeze "$helper_unit"
      # shellcheck disable=SC2317  # Called indirectly by the EXIT trap.
      thaw_helper() {
        systemctl --system thaw "$helper_unit" >/dev/null 2>&1 || true
        systemctl --system kill --kill-whom=all --signal=SIGCONT \
          "$helper_unit" >/dev/null 2>&1 || true
      }
      trap thaw_helper EXIT
      trap 'exit 129' HUP
      trap 'exit 130' INT
      trap 'exit 143' TERM
      if [[ "$crash_mode" == full-cgroup ]]; then
        systemctl --system stop "$recovery_unit" >/dev/null 2>&1 || true
      fi
      if cmp -s "$test_root/stale-pending" \
          /var/lib/vonk-forge/helper-upgrade.pending; then
        crash_pending_kind=stale
      elif cmp -s "$test_root/normalized-pending" \
          /var/lib/vonk-forge/helper-upgrade.pending; then
        crash_pending_kind=normalized
      else
        exit 1
      fi
      cp -- /var/lib/vonk-forge/helper-upgrade.pending \
        "$test_root/crash-point-pending"
      printf '%s\n' "$crash_pending_kind" > "$test_root/crash-point-pending-kind"
      dpkg_pid=$(sed -n 's/^dpkg_pid=//p' \
        /var/lib/vonk-forge/package-upgrade/intent)
      case "$dpkg_pid" in ''|*[!0-9]*) exit 1 ;; esac
      test "$(readlink -f "/proc/$dpkg_pid/exe")" = /usr/bin/dpkg
      mapfile -d '' -t dpkg_argv < "/proc/$dpkg_pid/cmdline"
      test "${#dpkg_argv[@]}" -eq 4
      test "${dpkg_argv[0]}" = /usr/bin/dpkg
      test "${dpkg_argv[1]}" = --install
      test "${dpkg_argv[2]}" = --force-confold
      test "${dpkg_argv[3]}" = "$candidate"
      crash_intent_digest=$(sha256sum \
        /var/lib/vonk-forge/package-upgrade/intent | cut -d' ' -f1)
      case "$crash_mode" in
        full-cgroup)
          # Keep every task stopped across the freezer release so dpkg cannot
          # advance before the complete cgroup is killed. Killing only after
          # thaw also avoids leaving systemd with a failed(frozen) test unit.
          helper_main_pid=$(systemctl --system show --property=MainPID --value \
            "$helper_unit")
          helper_control_group=$(systemctl --system show \
            --property=ControlGroup --value "$helper_unit")
          test "${helper_control_group:0:1}" = /
          test "${helper_control_group##*/}" = "$helper_unit"
          helper_cgroup_parent=${helper_control_group%/*}
          [[ "$helper_cgroup_parent" == /system.slice \
            || "$helper_cgroup_parent" == */system.slice ]]
          helper_cgroup=/sys/fs/cgroup$helper_control_group
          test -f "$helper_cgroup/cgroup.procs"
          mapfile -t helper_pids \
            < "$helper_cgroup/cgroup.procs"
          test "${#helper_pids[@]}" -ge 2
          printf '%s\n' "${helper_pids[@]}" | grep -Fxq "$helper_main_pid"
          printf '%s\n' "${helper_pids[@]}" | grep -Fxq "$dpkg_pid"
          systemctl --system kill --kill-whom=all --signal=SIGSTOP \
            "$helper_unit"
          systemctl --system thaw "$helper_unit"
          test "$(systemctl --system show --property=FreezerState --value \
            "$helper_unit")" = running
          for _ in {1..1000}; do
            all_helper_pids_quiescent=1
            for helper_pid in "${helper_pids[@]}"; do
              if [[ -r "/proc/$helper_pid/status" ]]; then
                helper_pid_state=
                while IFS=$' \t' read -r status_key status_value _; do
                  if [[ "$status_key" == State: ]]; then
                    helper_pid_state=$status_value
                    break
                  fi
                done < "/proc/$helper_pid/status" 2>/dev/null || continue
                case "$helper_pid_state" in
                  T|t|Z) ;;
                  D)
                    # A known atomic-text durability flush cannot execute
                    # userspace while blocked in D, and its queued SIGSTOP is
                    # delivered before it can return. Accept only the exact
                    # root-owned state paths reachable after intent rename.
                    helper_pid_exe=$(readlink -f "/proc/$helper_pid/exe" \
                      2>/dev/null || true)
                    helper_pid_argv=()
                    mapfile -d '' -t helper_pid_argv \
                      < "/proc/$helper_pid/cmdline" || true
                    safe_sync_target=0
                    if [[ "${#helper_pid_argv[@]}" -eq 3 ]]; then
                      case "${helper_pid_argv[2]}" in
                        /var/lib/vonk-forge/package-upgrade)
                          [[ -d "${helper_pid_argv[2]}" \
                            && ! -L "${helper_pid_argv[2]}" \
                            && "$(stat -c %u:%g:%a \
                              "${helper_pid_argv[2]}" 2>/dev/null || true)" \
                              == '0:0:700' ]] \
                            && safe_sync_target=1
                          ;;
                        /var/lib/vonk-forge)
                          [[ -d "${helper_pid_argv[2]}" \
                            && ! -L "${helper_pid_argv[2]}" \
                            && "$(stat -c %u:%g:%a \
                              "${helper_pid_argv[2]}" 2>/dev/null || true)" \
                              == '0:0:755' ]] \
                            && safe_sync_target=1
                          ;;
                        *)
                          if [[ "${helper_pid_argv[2]}" \
                            =~ ^/var/lib/vonk-forge(/package-upgrade)?/\.vonk-upgrade\.[0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz]{6}$ \
                            && -f "${helper_pid_argv[2]}" \
                            && ! -L "${helper_pid_argv[2]}" \
                            && "$(stat -c %u:%g:%a:%h \
                              "${helper_pid_argv[2]}" 2>/dev/null || true)" \
                              == '0:0:600:1' ]]; then
                            safe_sync_target=1
                          fi
                          ;;
                      esac
                    fi
                    safe_d_state=0
                    if [[ "$helper_pid_exe" != /usr/bin/sync \
                      || "${#helper_pid_argv[@]}" -ne 3 \
                      || "${helper_pid_argv[0]}" != /usr/bin/sync \
                      || "${helper_pid_argv[1]}" != -f \
                      || "$safe_sync_target" -ne 1 ]]; then
                      :
                    else
                      safe_d_state=1
                    fi
                    # dpkg may have a maintainer shell blocked in uninterruptible
                    # I/O while its own service stop waits on this frozen helper
                    # cgroup. The queued SIGSTOP is delivered before userspace can
                    # resume. Accept only the exact root-owned target preinst and
                    # exact upgrade arguments produced by this package fixture.
                    if [[ "$helper_pid_exe" == /usr/bin/dash \
                      && "${#helper_pid_argv[@]}" -eq 5 \
                      && "${helper_pid_argv[0]}" == /bin/sh \
                      && "${helper_pid_argv[1]}" == /var/lib/dpkg/tmp.ci/preinst \
                      && "${helper_pid_argv[2]}" == upgrade \
                      && "${helper_pid_argv[3]}" == "$baseline_version" \
                      && "${helper_pid_argv[4]}" == "$version" \
                      && -f /var/lib/dpkg/tmp.ci/preinst \
                      && ! -L /var/lib/dpkg/tmp.ci/preinst \
                      && "$(stat -c %u:%g:%a:%h \
                        /var/lib/dpkg/tmp.ci/preinst 2>/dev/null || true)" \
                        == '0:0:755:1' ]]; then
                      safe_d_state=1
                    fi
                    if [[ "$safe_d_state" -ne 1 ]]; then
                      all_helper_pids_quiescent=0
                    fi
                    ;;
                  *) all_helper_pids_quiescent=0 ;;
                esac
              fi
            done
            (( all_helper_pids_quiescent == 1 )) && break
            sleep 0.005
          done
          if (( all_helper_pids_quiescent != 1 )); then
            printf 'captured preinst stat=%s\n' \
              "$(stat -c %u:%g:%a:%h /var/lib/dpkg/tmp.ci/preinst \
                2>/dev/null || true)" >&2
            for helper_pid in "${helper_pids[@]}"; do
              [[ -r "/proc/$helper_pid/status" ]] || continue
              helper_pid_state=
              while IFS=$' \t' read -r status_key status_value _; do
                if [[ "$status_key" == State: ]]; then
                  helper_pid_state=$status_value
                  break
                fi
              done < "/proc/$helper_pid/status" 2>/dev/null || continue
              helper_pid_exe=$(readlink -f "/proc/$helper_pid/exe" \
                2>/dev/null || true)
              helper_pid_argv=()
              mapfile -d '' -t helper_pid_argv \
                < "/proc/$helper_pid/cmdline" || true
              printf 'captured helper pid=%s state=%s exe=%q argv=' \
                "$helper_pid" "$helper_pid_state" "$helper_pid_exe" >&2
              printf ' %q' "${helper_pid_argv[@]}" >&2
              printf '\n' >&2
            done
          fi
          test "$all_helper_pids_quiescent" -eq 1
          test -r "/proc/$dpkg_pid/status"
          dpkg_pid_state=
          while IFS=$' \t' read -r status_key status_value _; do
            if [[ "$status_key" == State: ]]; then
              dpkg_pid_state=$status_value
              break
            fi
          done < "/proc/$dpkg_pid/status" 2>/dev/null || exit 1
          case "$dpkg_pid_state" in T|t) ;; *) exit 1 ;; esac
          test "$(systemctl --system show --property=MainPID --value \
            "$helper_unit")" = "$helper_main_pid"
          test "$(sha256sum /var/lib/vonk-forge/package-upgrade/intent \
            | cut -d' ' -f1)" = "$crash_intent_digest"
          cmp -s "$test_root/crash-point-pending" \
            /var/lib/vonk-forge/helper-upgrade.pending
          assert_interrupted_baseline_state
          systemctl --system kill --kill-whom=all --signal=SIGKILL \
            "$helper_unit"
          for _ in {1..1000}; do
            helper_main_pid_after=$(systemctl --system show \
              --property=MainPID --value "$helper_unit")
            helper_active_state=$(systemctl --system show \
              --property=ActiveState --value "$helper_unit")
            helper_freezer_state=$(systemctl --system show \
              --property=FreezerState --value "$helper_unit")
            if [[ "$helper_main_pid_after" == 0 \
              && "$helper_active_state" == failed \
              && "$helper_freezer_state" == running ]]; then
              break
            fi
            sleep 0.005
          done
          test "$helper_main_pid_after" = 0
          test "$helper_active_state" = failed
          test "$helper_freezer_state" = running
          test "$(systemctl --system show --property=Result --value \
            "$helper_unit")" = signal
          cmp -s "$test_root/crash-point-pending" \
            /var/lib/vonk-forge/helper-upgrade.pending
          ;;
        dpkg-only|post-remove)
          test "$(systemctl --system show --property=FreezerState --value \
            "$helper_unit")" = frozen
          cmp -s "$test_root/crash-point-pending" \
            /var/lib/vonk-forge/helper-upgrade.pending
          test "$(sha256sum /var/lib/vonk-forge/package-upgrade/intent \
            | cut -d' ' -f1)" = "$crash_intent_digest"
          assert_interrupted_baseline_state
          systemctl --system daemon-reload
          systemctl --system --no-block start "$recovery_unit"
          kill -KILL "$dpkg_pid"
          # Recovery may auto-thaw the helper while stopping or restarting it;
          # normalize the freezer state without requiring a transient recovery
          # ActiveState or MainPID that a fast successful oneshot can outrun.
          for _ in {1..100}; do
            systemctl --system thaw "$helper_unit" \
              >/dev/null 2>&1 || true
            helper_freezer_state=$(systemctl --system show \
              --property=FreezerState --value "$helper_unit")
            if [[ "$helper_freezer_state" == running ]]; then
              break
            fi
            sleep 0.05
          done
          test "$helper_freezer_state" = running
          ;;
        *)
          exit 64
          ;;
      esac
      trap - EXIT HUP INT TERM
      touch "$crash_observed"
      exit 0
    fi
    sleep 0.005
  done
  exit 1
) &
crash_watcher=$!
touch "$started"

wait "$crash_watcher"
test -f "$crash_observed"
if [[ "$crash_mode" == full-cgroup ]]; then
  test -f /var/lib/vonk-forge/package-upgrade/intent
  cmp -s "$test_root/crash-point-pending" \
    /var/lib/vonk-forge/helper-upgrade.pending
  assert_interrupted_baseline_state

  # A boot-time start request cannot launch the old agent while the durable
  # intent is incomplete. A real boot does not preserve the test-only cgroup
  # freezer or failed-unit state, so release both explicitly before modelling
  # the static boot transaction without a controller retry.
  for _ in {1..100}; do
    systemctl --system thaw "$helper_unit" >/dev/null 2>&1 || true
    helper_freezer_state=$(systemctl --system show \
      --property=FreezerState --value "$helper_unit")
    [[ "$helper_freezer_state" == running ]] && break
    sleep 0.05
  done
  test "$helper_freezer_state" = running
  systemctl --system daemon-reload
  systemctl --system reset-failed \
    "$helper_unit" "$socket_unit" "$agent_unit" "$recovery_unit" \
    >/dev/null 2>&1 || true
  test "$(systemctl --system show --property=ActiveState --value \
    "$helper_unit")" = inactive
  test "$(systemctl --system show --property=Result --value \
    "$helper_unit")" = success
  if (( compatibility_trigger == 1 )); then
    # The staged recovery must reject corrupt root state before it stops the
    # still-running dev335-identity agent. Restore the exact intent only after
    # observing the permanent (78) failure.
    test "$(systemctl --system show --property=ActiveState --value \
      "$agent_unit")" = active
    old_agent_pid=$(systemctl --system show --property=MainPID --value \
      "$agent_unit")
    cp -- /var/lib/vonk-forge/package-upgrade/intent \
      "$test_root/compat-intent"
    grep -Fxq "runner_sha256=$target_recovery_runner_digest" \
      "$test_root/compat-intent"
    sed 's/^unit_sha256=.*/unit_sha256=ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff/' \
      "$test_root/compat-intent" > "$test_root/compat-intent.invalid"
    install -o root -g root -m 0600 "$test_root/compat-intent.invalid" \
      /var/lib/vonk-forge/package-upgrade/intent
    systemctl --system reset-failed "$recovery_unit" >/dev/null 2>&1 || true
    if systemctl --system start "$recovery_unit" >/dev/null 2>&1; then
      printf '%s\n' 'unsafe staged recovery unexpectedly succeeded' >&2
      exit 1
    fi
    test "$(systemctl --system show --property=ExecMainStatus --value \
      "$recovery_unit")" = 78
    test "$(systemctl --system show --property=MainPID --value \
      "$agent_unit")" = "$old_agent_pid"
    test "$(systemctl --system show --property=ActiveState --value \
      "$agent_unit")" = active
    install -o root -g root -m 0600 "$test_root/compat-intent" \
      /var/lib/vonk-forge/package-upgrade/intent
    sync -f /var/lib/vonk-forge/package-upgrade/intent
    sync -f /var/lib/vonk-forge/package-upgrade

    # Run the exact dev335 helper binary against the active production-shaped
    # socket, while leaving the fully staged target payload on disk for the
    # root recovery owner. The fixed Rust fixture signs only a 60-second
    # ScheduleReboot grant, proves the response, then replays that same grant.
    install -d -o root -g root -m 0700 /var/lib/vonk-forge/helper/requests
    install -d -o root -g root -m 0755 /etc/vonk-forge-agent
    printf '%s\n' \
      8b237d788e8eaaef550c6d125823fa45f1fd5fc29b2c88bdf871119471fc1312 \
      > /etc/vonk-forge-agent/host-helper-authority.pub
    chown root:root /etc/vonk-forge-agent/host-helper-authority.pub
    chmod 0644 /etc/vonk-forge-agent/host-helper-authority.pub
    printf 'node_id = "%s"\n' \
      spk_2818d189042b4c77aefa7796f4befd23 \
      > /etc/vonk-forge-agent/agent.toml
    chown root:root /etc/vonk-forge-agent/agent.toml
    chmod 0644 /etc/vonk-forge-agent/agent.toml
    cp -- /usr/lib/vonk-forge/vonk-agent-helper \
      "$test_root/staged-target-helper"
    cp -- "$old_socket_unit" "$test_root/staged-target-helper.socket"
    sed '/^Wants=vonk-forge-package-upgrade-recover.service$/d' \
      "$test_root/staged-target-helper.socket" \
      > "$test_root/pre-trigger-helper.socket"
    install -o root -g root -m 0644 "$test_root/pre-trigger-helper.socket" \
      "$old_socket_unit"
    if [[ -f /lib/systemd/system/vonk-forge-package-helper.socket.d/20-package-upgrade-recovery.conf ]]; then
      mv -- /lib/systemd/system/vonk-forge-package-helper.socket.d/20-package-upgrade-recovery.conf \
        "$test_root/staged-target-helper.socket-dropin"
    fi
    sed 's#^ExecStart=.*#ExecStart=/usr/lib/vonk-forge/vonk-agent-helper#' \
      "$old_helper_unit" > "$test_root/dev335-helper.service"
    grep -Fxq 'ExecStart=/usr/lib/vonk-forge/vonk-agent-helper' \
      "$test_root/dev335-helper.service"
    install -o root -g root -m 0644 "$test_root/dev335-helper.service" \
      "$old_helper_unit"
    install -o root -g root -m 0555 \
      "$test_root/baseline-bin/vonk-agent-helper" \
      /usr/lib/vonk-forge/vonk-agent-helper
    # Killing the synthetic dpkg fixture may restore its baseline data file
    # after the target preinst durably records the target runner. Spark3542's
    # pinned authority instead proves the exact staged a122 runner is present,
    # so restore and prove that observed state before exercising its one-shot
    # compatibility grant.
    install -o root -g root -m 0555 "$target_recovery_runner" \
      /usr/lib/vonk-forge/vonk-forge-package-upgrade-recover
    test "$(sha256sum \
      /usr/lib/vonk-forge/vonk-forge-package-upgrade-recover \
      | cut -d' ' -f1)" = "$target_recovery_runner_digest"
    systemctl --system daemon-reload
    systemctl --system reset-failed "$helper_unit" "$recovery_unit" \
      >/dev/null 2>&1 || true
    systemctl --system start "$helper_unit"
    compat_helper_pid=$(systemctl --system show --property=MainPID --value \
      "$helper_unit")
    test "$(sha256sum "/proc/$compat_helper_pid/exe" | cut -d' ' -f1)" \
      = "$dev335_helper_digest"
    install -o root -g root -m 0555 "$test_root/staged-target-helper" \
      /usr/lib/vonk-forge/vonk-agent-helper
    install -o root -g root -m 0644 "$test_root/staged-target-helper.socket" \
      "$old_socket_unit"
    if [[ -f "$test_root/staged-target-helper.socket-dropin" ]]; then
      install -d -o root -g root -m 0755 \
        /lib/systemd/system/vonk-forge-package-helper.socket.d
      install -o root -g root -m 0644 \
        "$test_root/staged-target-helper.socket-dropin" \
        /lib/systemd/system/vonk-forge-package-helper.socket.d/20-package-upgrade-recovery.conf
    fi
    systemctl --system daemon-reload
    vonk_agent_gid=$(getent group vonk-agent | cut -d: -f3)

    # A pre-existing transient name must make the exact dev335 helper reject
    # its consumed request without scheduling a reboot or falling back to a
    # direct systemctl action.
    systemd-run --quiet --collect \
      --unit=vonk-forge-reboot.service /bin/sleep 30
    CARGO_TARGET_DIR=$test_root/compat-target \
      VONK_SPARK3542_COMPAT_FIXTURE=1 \
      setpriv --reuid 0 --regid "$vonk_agent_gid" --clear-groups \
      cargo test --locked --manifest-path "$repo_root/Cargo.toml" \
        -p vonk-agent-helper --test spark3542_compat_reboot_fixture \
        transient_reboot_unit_collision_fails_closed_for_the_second_pinned_request \
        -- --ignored --exact
    test "$(systemctl --system show --property=MainPID --value \
      "$helper_unit")" = "$compat_helper_pid"
    test "$(systemctl --system show --property=ActiveState --value \
      "$recovery_unit")" != active
    systemctl --system stop vonk-forge-reboot.service
    systemctl --system reset-failed vonk-forge-reboot.service \
      >/dev/null 2>&1 || true
    for _ in {1..100}; do
      [[ "$(systemctl --system show --property=LoadState --value \
        "$compat_reboot_service" 2>/dev/null || true)" == not-found ]] && break
      sleep 0.05
    done
    test "$(systemctl --system show --property=LoadState --value \
      "$compat_reboot_service")" = not-found
    rm -f -- "$compat_collision_ledger"

    # Deliver while the old agent is still active. Hold dpkg's native advisory
    # lock, cancel the actual transient reboot, then model its boot transaction.
    # Starting the enabled socket must pull recovery, first exit 75 under the
    # held lock, and then converge after lock release.
    test "$(systemctl --system show --property=ActiveState --value \
      "$agent_unit")" = active
    delivered_agent_pid=$(systemctl --system show --property=MainPID --value \
      "$agent_unit")
    test "$delivered_agent_pid" = "$old_agent_pid"
    python3 - "$test_root/dpkg-lock-ready" \
      "$test_root/release-dpkg-lock" <<'PY' &
import fcntl
import pathlib
import sys
import time

ready = pathlib.Path(sys.argv[1])
release = pathlib.Path(sys.argv[2])
with pathlib.Path("/var/lib/dpkg/lock").open("r+") as lock:
    fcntl.lockf(lock, fcntl.LOCK_EX)
    ready.touch()
    while not release.exists():
        time.sleep(0.05)
PY
    dpkg_lock_pid=$!
    for _ in {1..200}; do
      [[ -f "$test_root/dpkg-lock-ready" ]] && break
      sleep 0.05
    done
    test -f "$test_root/dpkg-lock-ready"
    CARGO_TARGET_DIR=$test_root/compat-target \
      VONK_SPARK3542_COMPAT_FIXTURE=1 \
      setpriv --reuid 0 --regid "$vonk_agent_gid" --clear-groups \
      cargo test --locked --manifest-path "$repo_root/Cargo.toml" \
        -p vonk-agent-helper --test spark3542_compat_reboot_fixture \
        sends_only_one_pinned_reboot_and_replay_is_rejected \
        -- --ignored --exact
    test "$(stat -c '%U:%G:%a:%h' "$compat_request_ledger")" = root:root:600:1
    cmp -s <(printf 'pending\n') "$compat_request_ledger"
    test ! -e "$compat_collision_ledger"
    test "$(systemctl --system show --property=ActiveState --value \
      "$compat_reboot_timer")" = active
    systemctl --system stop "$compat_reboot_timer" "$compat_reboot_service" \
      >/dev/null 2>&1 || true
    systemctl --system reset-failed \
      "$compat_reboot_timer" "$compat_reboot_service" \
      >/dev/null 2>&1 || true
    systemctl --system stop \
      "$agent_unit" "$helper_unit" "$socket_unit" "$recovery_unit" \
      >/dev/null 2>&1 || true
    systemctl --system daemon-reload
    systemctl --system reset-failed \
      "$agent_unit" "$helper_unit" "$socket_unit" "$recovery_unit" \
      >/dev/null 2>&1 || true
    systemctl --system start "$socket_unit"
    for _ in {1..150}; do
      recovery_exit=$(systemctl --system show --property=ExecMainStatus --value \
        "$recovery_unit")
      [[ "$recovery_exit" == 75 ]] && break
      sleep 0.1
    done
    test "$recovery_exit" = 75
    test "$(systemctl --system show --property=ActiveState --value \
      "$agent_unit")" = inactive
    touch "$test_root/release-dpkg-lock"
    wait "$dpkg_lock_pid"
    dpkg_lock_pid=
  else
    systemctl --system start "$agent_unit" >/dev/null 2>&1 || true
    test "$(systemctl --system show --property=ActiveState --value \
      "$agent_unit")" != active
    systemctl --system stop "$socket_unit" >/dev/null
    systemctl --system start "$socket_unit" >/dev/null
  fi
fi
if [[ "$crash_mode" == post-remove ]]; then
  # The synthetic postinst forces recovery into its package-scoped remove and
  # reinstall fallback. Its synthetic preinst then blocks only after removal,
  # giving this fixture a deterministic power-loss boundary.
  for _ in {1..1200}; do
    if [[ -f /var/lib/vonk-forge/package-upgrade/test-post-remove-preinst-entered ]] \
      && grep -Fxq 'stage=package-reinstall' \
        /var/lib/vonk-forge/package-upgrade.status 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  test -f /var/lib/vonk-forge/package-upgrade/test-post-remove-preinst-entered
  grep -Fxq 'stage=package-reinstall' \
    /var/lib/vonk-forge/package-upgrade.status

  # A power loss kills the complete recovery cgroup. Cancel systemd's delayed
  # automatic retry so the assertions below observe the exact post-remove
  # durable state before modelling the next boot transaction.
  systemctl --system kill --kill-whom=all --signal=SIGKILL "$recovery_unit"
  systemctl --system stop "$recovery_unit" >/dev/null 2>&1 || true
  for _ in {1..200}; do
    recovery_active_state=$(systemctl --system show \
      --property=ActiveState --value "$recovery_unit")
    recovery_main_pid=$(systemctl --system show \
      --property=MainPID --value "$recovery_unit")
    [[ "$recovery_active_state" == inactive \
      && "$recovery_main_pid" == 0 ]] && break
    sleep 0.05
  done
  test "$recovery_active_state" = inactive
  test "$recovery_main_pid" = 0
  test ! -e /usr/lib/vonk-forge/vonk-forge-package-upgrade-recover
  test ! -e /lib/systemd/system/vonk-forge-package-upgrade-recover.service
  test -f /var/lib/vonk-forge/package-upgrade/intent
  test -f "/var/lib/vonk-forge/package-upgrade/$package_digest.deb"
  test -f /var/lib/vonk-forge/package-upgrade/recovery-capsule/runner
  test -f "$recovery_unit_path"
  test -L "$recovery_enablement"
  test "$(readlink "$recovery_enablement")" = "$recovery_unit_path"
  test -f "$recovery_gate"
  test -f "$recovery_suppression"

  # dpkg has completed the remove half of the fallback. Re-stage only the
  # service metadata needed to model the next boot; this deliberately does not
  # restore the agent binary or package database entry. The recovery drop-in
  # must be the reason an early agent start is rejected.
  test "$(dpkg-query -W -f='${db:Status-Abbrev}' vonk-forge-agent \
    | cut -c1-2)" != ii
  agent_restaged="$test_root/restaged-agent.service"
  cat > "$agent_restaged" <<'UNIT'
[Unit]
Description=Vonk Forge recovery fixture agent
After=local-fs.target

[Service]
Type=simple
User=vonk-agent
Group=vonk-agent
ExecStart=/bin/sleep 3600

[Install]
WantedBy=multi-user.target
UNIT
install -o root -g root -m 0644 "$agent_restaged" "$agent_unit_path"
install -d -o root -g root -m 0755 \
    /lib/systemd/system/multi-user.target.wants
ln -s "../$agent_unit" "$agent_enablement"
systemctl --system daemon-reload
test "$(systemctl --system show --property=FragmentPath --value \
  "$agent_unit")" = "$agent_unit_path"
agent_dropins=$(systemctl --system show --property=DropInPaths --value \
  "$agent_unit")
case " $agent_dropins " in
  *" $recovery_gate "*) ;;
  *) printf '%s\n' 'agent recovery gate is not an installed drop-in' >&2; exit 1 ;;
esac
test "$(systemctl --system is-enabled "$agent_unit")" = enabled
test "$(systemctl --system show --property=ActiveState --value \
  multi-user.target)" = active
test "$(stat -c %u:%g:%a:%h \
  "/var/lib/vonk-forge/package-upgrade/$package_digest.deb")" = 0:0:600:1
test "$(sha256sum "/var/lib/vonk-forge/package-upgrade/$package_digest.deb" \
  | cut -d' ' -f1)" = "$package_digest"
capsule_unit_digest=$(sha256sum "$recovery_unit_path" | cut -d' ' -f1)
capsule_gate_digest=$(sha256sum "$recovery_gate" | cut -d' ' -f1)
capsule_suppression_digest=$(sha256sum "$recovery_suppression" \
  | cut -d' ' -f1)
test "$capsule_unit_digest" = "$(dpkg-deb --fsys-tarfile \
  "/var/lib/vonk-forge/package-upgrade/$package_digest.deb" \
  | tar -xOf - ./lib/systemd/system/$recovery_unit \
  | sha256sum | cut -d' ' -f1)"
test "$capsule_gate_digest" = "$(dpkg-deb --fsys-tarfile \
  "/var/lib/vonk-forge/package-upgrade/$package_digest.deb" \
  | tar -xOf - ./lib/systemd/system/vonk-forge-agent.service.d/10-package-upgrade-capsule.conf \
  | sha256sum | cut -d' ' -f1)"
test "$capsule_suppression_digest" = "$(dpkg-deb --fsys-tarfile \
  "/var/lib/vonk-forge/package-upgrade/$package_digest.deb" \
  | tar -xOf - ./lib/systemd/system/$package_recovery_unit.d/10-capsule-owner.conf \
  | sha256sum | cut -d' ' -f1)"
test "$(sha256sum "/var/lib/vonk-forge/package-upgrade/recovery-capsule/runner" \
  | cut -d' ' -f1)" = "$target_recovery_runner_digest"

  systemctl --system daemon-reload
  systemctl --system reset-failed "$agent_unit" "$recovery_unit" \
    >/dev/null 2>&1 || true
  if systemctl --system start "$agent_unit" >/dev/null 2>&1; then
    printf '%s\n' 'agent unexpectedly started before capsule recovery' >&2
    exit 1
  fi
  test "$(systemctl --system show --property=ActiveState --value \
    "$agent_unit")" != active

  # multi-user.target owns the exact durable wants symlink. Starting the
  # enabled capsule directly is the systemd boot transaction without rebooting
  # the disposable CI host itself.
  # Starting the target is the boot-equivalent transaction. The explicit
  # capsule start is retained as a deterministic fallback for hosts whose
  # multi-user target was already active when the fixture entered this gate.
  systemctl --system start multi-user.target
  systemctl --system start "$recovery_unit"
fi
printf 'durable recovery crash-point pending gate: %s\n' \
  "$(cat "$test_root/crash-point-pending-kind")"

for _ in {1..1200}; do
  recovery_load_state=$(systemctl --system show --property=LoadState --value \
    "$recovery_unit" 2>/dev/null || true)
  recovery_active_state=$(systemctl --system show --property=ActiveState --value \
    "$recovery_unit" 2>/dev/null || true)
  recovery_sub_state=$(systemctl --system show --property=SubState --value \
    "$recovery_unit" 2>/dev/null || true)
  recovery_main_pid=$(systemctl --system show --property=MainPID --value \
    "$recovery_unit" 2>/dev/null || true)
  package_recovery_active_state=$(systemctl --system show \
    --property=ActiveState --value "$package_recovery_unit")
  package_recovery_sub_state=$(systemctl --system show \
    --property=SubState --value "$package_recovery_unit")
  if [[ -f /var/lib/vonk-forge/helper-upgrade.receipt \
    && ! -e /var/lib/vonk-forge/helper-upgrade.pending \
    && ! -e /var/lib/vonk-forge/package-upgrade/intent \
    && ! -e /var/lib/vonk-forge/package-upgrade/agent-blocked \
    && ! -e "/var/lib/vonk-forge/package-upgrade/$package_digest.deb" \
    && "$recovery_load_state" == not-found \
    && "$recovery_active_state" == inactive \
    && "$recovery_sub_state" == dead \
    && "$recovery_main_pid" == 0 \
    && "$package_recovery_active_state" == inactive \
    && "$package_recovery_sub_state" == dead ]]; then
    break
  fi
  sleep 0.1
done

test "$recovery_load_state" = not-found
test "$recovery_active_state" = inactive
test "$recovery_sub_state" = dead
test "$recovery_main_pid" = 0
test "$package_recovery_active_state" = inactive
test "$package_recovery_sub_state" = dead
grep -Fxq 'schema_version=2' /var/lib/vonk-forge/helper-upgrade.receipt
grep -Fxq "version=$version" /var/lib/vonk-forge/helper-upgrade.receipt
grep -Fxq "package_sha256=$package_digest" \
  /var/lib/vonk-forge/helper-upgrade.receipt
test "$(stat -c %U:%G:%a:%h /var/lib/vonk-forge/package-upgrade.status)" \
  = root:root:644:1
test "$(wc -l < /var/lib/vonk-forge/package-upgrade.status)" -eq 7
grep -Fxq 'schema_version=1' /var/lib/vonk-forge/package-upgrade.status
grep -Fxq 'outcome=succeeded' /var/lib/vonk-forge/package-upgrade.status
grep -Fxq 'stage=complete' /var/lib/vonk-forge/package-upgrade.status
grep -Fxq 'reason=exact_identity_proven' \
  /var/lib/vonk-forge/package-upgrade.status
grep -Fxq "target_version=$version" /var/lib/vonk-forge/package-upgrade.status
grep -Fxq "package_sha256=$package_digest" \
  /var/lib/vonk-forge/package-upgrade.status
test "$(wc -l < "$upgrade_invocations")" -eq 1
test ! -e /var/lib/vonk-forge/helper-upgrade.pending
test ! -e /var/lib/vonk-forge/package-upgrade/intent
test ! -e /var/lib/vonk-forge/package-upgrade/agent-blocked
test ! -e "/var/lib/vonk-forge/package-upgrade/$package_digest.deb"
test "$(dpkg-query -W -f='${db:Status-Abbrev}' vonk-forge-agent | cut -c1-2)" = ii
test "$(dpkg-query -W -f='${Version}' vonk-forge-agent)" = "$version"
test "$(systemctl --system show --property=ActiveState --value "$helper_unit")" = active
test "$(systemctl --system show --property=ActiveState --value "$agent_unit")" = active
helper_pid=$(systemctl --system show --property=MainPID --value "$helper_unit")
agent_pid=$(systemctl --system show --property=MainPID --value "$agent_unit")
test "$(sha256sum "/proc/$helper_pid/exe" | cut -d' ' -f1)" = "$helper_digest"
test "$(sha256sum "/proc/$agent_pid/exe" | cut -d' ' -f1)" = "$agent_digest"
test "$(stat -c %U "/proc/$agent_pid")" = vonk-agent
if (( compatibility_trigger == 1 )); then
  for _ in {1..100}; do
    reboot_service_load=$(systemctl --system show --property=LoadState --value \
      "$compat_reboot_service" 2>/dev/null || true)
    reboot_timer_load=$(systemctl --system show --property=LoadState --value \
      "$compat_reboot_timer" 2>/dev/null || true)
    [[ "$reboot_service_load" == not-found \
      && "$reboot_timer_load" == not-found ]] && break
    sleep 0.05
  done
  test "$(systemctl --system show --property=LoadState --value \
    "$compat_reboot_service")" = not-found
  test "$(systemctl --system show --property=LoadState --value \
    "$compat_reboot_timer")" = not-found
fi

# A subsequent ordinary live reinstall must accept and replace the durable v2
# receipt. Package removal must then accept the normal v1 receipt.
stage_candidate
dpkg --install --force-confold "$candidate"
for _ in {1..1200}; do
  if grep -Fxq 'schema_version=1' \
      /var/lib/vonk-forge/helper-upgrade.receipt 2>/dev/null \
    && [[ ! -e /var/lib/vonk-forge/helper-upgrade.pending ]]; then
    break
  fi
  sleep 0.1
done
grep -Fxq 'schema_version=1' /var/lib/vonk-forge/helper-upgrade.receipt
dpkg --remove vonk-forge-agent
test ! -e /var/lib/vonk-forge/helper-upgrade.receipt

printf '%s\n' \
  "durable lower-interrupted $stale_pending_format $crash_mode $candidate_custody recovery/normal-upgrade/remove lifecycle: PASS"

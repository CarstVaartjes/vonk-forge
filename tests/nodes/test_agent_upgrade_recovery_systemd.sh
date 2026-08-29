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
case "$candidate_custody" in
  legacy|root) ;;
  *) printf 'unknown candidate custody fixture: %s\n' "$candidate_custody" >&2; exit 64 ;;
esac
case "$(dpkg --print-architecture)" in
  amd64) build_arch=linux-amd64 ;;
  arm64) build_arch=linux-arm64 ;;
  *) printf '%s\n' 'unsupported recovery test architecture' >&2; exit 77 ;;
esac

test_root="$(mktemp -d /var/lib/vonk-forge-recovery-test.XXXXXX)"
old_helper_unit=/lib/systemd/system/vonk-forge-package-helper.service
old_socket_unit=/lib/systemd/system/vonk-forge-package-helper.socket
helper_unit=vonk-forge-package-helper.service
socket_unit=vonk-forge-package-helper.socket
agent_unit=vonk-forge-agent.service
recovery_unit=vonk-forge-package-upgrade-recover.service
firewall_unit=vonk-forge-docker-firewall.service
firewall_fixture=/run/systemd/system/$firewall_unit
started=$test_root/start
crash_observed=$test_root/crash-observed
# The simulated old helper can write only its declared ReadWritePaths.
upgrade_invocations=/var/lib/vonk-forge/upgrade-invocations.$(basename "$test_root")

dump_failure_diagnostics() {
  printf '%s\n' '--- agent upgrade recovery fixture diagnostics ---' >&2
  systemctl --system --no-pager --full status \
    "$helper_unit" "$socket_unit" "$recovery_unit" "$agent_unit" \
    "$firewall_unit" >&2 || true
  journalctl --system --no-pager -n 200 \
    -u "$helper_unit" -u "$socket_unit" -u "$recovery_unit" -u "$agent_unit" \
    -u "$firewall_unit" \
    >&2 || true
  dpkg-query -W -f='${db:Status-Abbrev} ${Version}\n' vonk-forge-agent \
    >&2 || true
  for state_file in \
    /var/lib/vonk-forge/package-upgrade/intent \
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
  systemctl --system thaw "$helper_unit" >/dev/null 2>&1 || true
  systemctl --system stop "$recovery_unit" "$agent_unit" "$helper_unit" \
    "$socket_unit" "$firewall_unit" >/dev/null 2>&1 || true
  systemctl --system reset-failed "$recovery_unit" "$agent_unit" \
    "$helper_unit" "$socket_unit" "$firewall_unit" >/dev/null 2>&1 || true
  rm -rf -- /var/lib/vonk-forge/package-upgrade
  rm -f -- /var/lib/vonk-forge/helper-upgrade.pending \
    /var/lib/vonk-forge/helper-upgrade.receipt
  if dpkg-query --show vonk-forge-agent >/dev/null 2>&1; then
    SYSTEMD_OFFLINE=1 dpkg --purge --force-remove-reinstreq \
      vonk-forge-agent >/dev/null 2>&1 || cleanup_status=1
  fi
  if dpkg-query --show vonk-forge-agent >/dev/null 2>&1; then
    cleanup_status=1
  fi
  rm -rf -- /var/lib/vonk-forge-agent
  rm -f -- "$upgrade_invocations"
  rm -f -- "$old_helper_unit" "$old_socket_unit"
  rm -f -- "$firewall_fixture"
  rm -rf -- /lib/systemd/system/vonk-forge-package-helper.socket.d
  systemctl --system daemon-reload >/dev/null 2>&1 || true
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
  || systemctl --system cat "$firewall_unit" >/dev/null 2>&1 \
  || [[ -e /var/lib/vonk-forge/package-upgrade \
    || -L /var/lib/vonk-forge/package-upgrade ]] \
  || [[ -e /var/lib/vonk-forge/helper-upgrade.pending \
    || -L /var/lib/vonk-forge/helper-upgrade.pending ]] \
  || [[ -e /var/lib/vonk-forge/helper-upgrade.receipt \
    || -L /var/lib/vonk-forge/helper-upgrade.receipt ]] \
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
cp -- "$test_root/target-bin/vonk-agent-helper" \
  "$test_root/baseline-bin/vonk-agent-helper"
for binary_dir in "$test_root/target-bin" "$test_root/baseline-bin"; do
  cp -- "$binary_dir/vonk-agent-helper" "$binary_dir/oras"
  printf '%s\n' 'ORAS recovery fixture license' > "$binary_dir/oras.LICENSE"
  chmod 0555 "$binary_dir/vonk-agent" "$binary_dir/vonk-agent-helper" \
    "$binary_dir/oras"
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
package_digest=$(sha256sum "$package" | cut -d' ' -f1)
agent_digest=$(sha256sum "$test_root/target-bin/vonk-agent" | cut -d' ' -f1)
helper_digest=$(sha256sum "$test_root/target-bin/vonk-agent-helper" | cut -d' ' -f1)
baseline_agent_digest=$(sha256sum "$test_root/baseline-bin/vonk-agent" \
  | cut -d' ' -f1)

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

# Kill the complete old-helper cgroup after durable intent commit. Merely
# observing the intent pathname can race the directory fsync inside atomic_text.
# The later agent blocker proves the intent commit returned; a short quiescent
# window with no exact state-directory sync descendant then avoids freezing the
# blocker's own fsync. The target preinst is allowed to normalize the safe stale
# gate before the watcher runs, so capture and prove whichever exact safe state
# exists at the crash point.
(
  intent_sync_quiescent=0
  for _ in {1..2400}; do
    if [[ -f /var/lib/vonk-forge/package-upgrade/intent \
      && -f /var/lib/vonk-forge/package-upgrade/agent-blocked ]]; then
      helper_control_group=$(systemctl --system show \
        --property=ControlGroup --value "$helper_unit")
      case "$helper_control_group" in
        /system.slice/*) ;;
        *) exit 1 ;;
      esac
      intent_sync_active=0
      mapfile -t intent_sync_pids \
        < "/sys/fs/cgroup$helper_control_group/cgroup.procs"
      for intent_sync_pid in "${intent_sync_pids[@]}"; do
        intent_sync_argv=()
        if [[ -r "/proc/$intent_sync_pid/cmdline" ]] \
          && mapfile -d '' -t intent_sync_argv \
            < "/proc/$intent_sync_pid/cmdline" \
          && [[ "${#intent_sync_argv[@]}" -eq 3 \
            && "${intent_sync_argv[0]}" = /usr/bin/sync \
            && "${intent_sync_argv[1]}" = -f \
            && "${intent_sync_argv[2]}" \
              = /var/lib/vonk-forge/package-upgrade ]]; then
          intent_sync_active=1
          break
        fi
      done
      if (( intent_sync_active == 1 )); then
        intent_sync_quiescent=0
        sleep 0.005
        continue
      fi
      intent_sync_quiescent=$((intent_sync_quiescent + 1))
      if (( intent_sync_quiescent < 5 )); then
        sleep 0.005
        continue
      fi
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
          case "$helper_control_group" in
            /system.slice/*) ;;
            *) exit 1 ;;
          esac
          mapfile -t helper_pids \
            < "/sys/fs/cgroup$helper_control_group/cgroup.procs"
          test "${#helper_pids[@]}" -ge 2
          printf '%s\n' "${helper_pids[@]}" | grep -Fxq "$helper_main_pid"
          printf '%s\n' "${helper_pids[@]}" | grep -Fxq "$dpkg_pid"
          systemctl --system kill --kill-whom=all --signal=SIGSTOP \
            "$helper_unit"
          systemctl --system thaw "$helper_unit"
          test "$(systemctl --system show --property=FreezerState --value \
            "$helper_unit")" = running
          for _ in {1..1000}; do
            all_helper_pids_stopped=1
            for helper_pid in "${helper_pids[@]}"; do
              if [[ -r "/proc/$helper_pid/status" ]]; then
                helper_pid_state=$(sed -n \
                  's/^State:[[:space:]]*\([A-Za-z]\).*/\1/p' \
                  "/proc/$helper_pid/status")
                case "$helper_pid_state" in
                  T|t) ;;
                  *) all_helper_pids_stopped=0 ;;
                esac
              fi
            done
            (( all_helper_pids_stopped == 1 )) && break
            sleep 0.005
          done
          test "$all_helper_pids_stopped" -eq 1
          test -r "/proc/$dpkg_pid/status"
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
        dpkg-only)
          systemctl --system daemon-reload
          systemctl --system --no-block start "$recovery_unit"
          kill -KILL "$dpkg_pid"
          sleep 1
          test "$(systemctl --system show --property=ActiveState --value \
            "$recovery_unit")" = activating
          test "$(systemctl --system show --property=MainPID --value \
            "$recovery_unit")" -gt 0
          test "$(systemctl --system show --property=FreezerState --value \
            "$helper_unit")" = frozen
          test "$(sha256sum /var/lib/vonk-forge/package-upgrade/intent \
            | cut -d' ' -f1)" = "$crash_intent_digest"
          assert_interrupted_baseline_state
          # The surviving preinst that may normalize this gate is still frozen.
          # Preserve the exact safe state captured before killing its dpkg parent.
          cmp -s "$test_root/crash-point-pending" \
            /var/lib/vonk-forge/helper-upgrade.pending
          # Recovery may auto-thaw the helper while stopping or restarting it;
          # the observed freezer state, not a later thaw command, is invariant.
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
test -f /var/lib/vonk-forge/package-upgrade/intent
if [[ "$crash_mode" == full-cgroup ]]; then
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
  systemctl --system start "$agent_unit" >/dev/null 2>&1 || true
  test "$(systemctl --system show --property=ActiveState --value "$agent_unit")" \
    != active
  systemctl --system stop "$socket_unit" >/dev/null
  systemctl --system start "$socket_unit" >/dev/null
fi
printf 'durable recovery crash-point pending gate: %s\n' \
  "$(cat "$test_root/crash-point-pending-kind")"

for _ in {1..1200}; do
  if [[ -f /var/lib/vonk-forge/helper-upgrade.receipt \
    && ! -e /var/lib/vonk-forge/helper-upgrade.pending \
    && ! -e /var/lib/vonk-forge/package-upgrade/intent ]]; then
    break
  fi
  sleep 0.1
done

grep -Fxq 'schema_version=2' /var/lib/vonk-forge/helper-upgrade.receipt
grep -Fxq "version=$version" /var/lib/vonk-forge/helper-upgrade.receipt
grep -Fxq "package_sha256=$package_digest" \
  /var/lib/vonk-forge/helper-upgrade.receipt
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

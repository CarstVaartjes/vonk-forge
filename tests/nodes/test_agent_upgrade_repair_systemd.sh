#!/usr/bin/env bash
set -euo pipefail

# Native, destructive-on-an-empty-runner acceptance for the node-bound repair
# capsule.  Each invocation owns the host package/unit names, so it refuses to
# run if any Vonk package or state already exists.

if (( EUID != 0 )); then
  printf '%s\n' 'agent repair lifecycle test must run as root' >&2
  exit 77
fi
if [[ ! -d /run/systemd/system ]]; then
  printf '%s\n' 'agent repair lifecycle test requires systemd' >&2
  exit 77
fi
if [[ "$(dpkg --print-architecture)" != arm64 ]]; then
  printf '%s\n' 'agent repair lifecycle test requires native arm64' >&2
  exit 77
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fault=${REPAIR_FAULT:-none}
crash_phase=${REPAIR_CRASH_PHASE:-none}
standard_residue=${REPAIR_STANDARD_RESIDUE:-none}
node_id=spk_2818d189042b4c77aefa7796f4befd23
node_suffix=${node_id#spk_}
installed_version=0.1.0~dev.335+g2eaaf4d9b2b5
source_version=0.1.0~dev.381+ga122909feaa3
repair_version=${source_version}+repair.spk${node_suffix}.1
ordinary_version=0.1.0~dev.382+g0123456789ab
binary_revision=a122909feaa3b64d7b15371285e727965c3d7e9a
packaging_revision="$(git -c safe.directory="$repo_root" --no-replace-objects -C "$repo_root" rev-parse HEAD)"
epoch="$(git -c safe.directory="$repo_root" --no-replace-objects -C "$repo_root" show -s --format=%ct HEAD)"

repair_crash_phases=(armed installing configured helper-proven agent-proven)
repair_boot_crashpoints=(
  pre-runner-rename post-runner-rename helper-proven-boot agent-proven-boot
)
repair_faults=(
  none wrong-node config-mode config-symlink direct-dpkg
  dpkg-iU dpkg-iF dpkg-iHR absent newer
  installed-agent installed-helper installed-agent-unit
  installed-helper-unit installed-socket-unit
  running-agent running-helper cgroup-agent cgroup-helper
  source-intent source-cache source-runner source-unit source-gate
  source-dropin source-blocker source-pending source-lock-busy
)
if [[ "$crash_phase" != none ]]; then
  case " ${repair_crash_phases[*]} ${repair_boot_crashpoints[*]} " in
    *" $crash_phase "*) ;;
    *)
    printf 'unknown repair crash phase: %s\n' "$crash_phase" >&2
    exit 64
  esac
fi
case " ${repair_faults[*]} " in *" $fault "*) ;; *)
  printf 'unknown repair adversarial fixture: %s\n' "$fault" >&2
  exit 64
esac
if [[ "$fault" != none && "$crash_phase" != none ]]; then
  printf '%s\n' 'repair fault and crash fixtures are mutually exclusive' >&2
  exit 64
fi
case "$standard_residue" in none|exact-0755) ;; *)
  printf 'unknown standard delegate residue fixture: %s\n' \
    "$standard_residue" >&2
  exit 64
esac
if [[ "$standard_residue" != none \
  && ( "$fault" != none || "$crash_phase" != none ) ]]; then
  printf '%s\n' 'delegate residue, repair fault, and crash fixtures are mutually exclusive' >&2
  exit 64
fi

test_root=
firewall_unit=/run/systemd/system/vonk-forge-docker-firewall.service
trigger=/var/lib/vonk-forge/test-helper-install.trigger
trigger_stage=/var/lib/vonk-forge/test-helper-install.pending
result=/var/lib/vonk-forge/test-helper-install.result
started=/var/lib/vonk-forge/test-helper-install.started
source_state=/var/lib/vonk-forge/package-upgrade
source_intent=$source_state/intent
source_lock=$source_state/lock
source_blocker=$source_state/agent-blocked
source_pending=/var/lib/vonk-forge/helper-upgrade.pending
source_runner=/usr/lib/vonk-forge/vonk-forge-package-upgrade-recover
standard_runner=/usr/lib/vonk-forge/vonk-forge-package-upgrade-recover.standard
source_unit=/lib/systemd/system/vonk-forge-package-upgrade-recover.service
source_gate=/lib/systemd/system/vonk-forge-agent.service.d/20-package-upgrade-recovery.conf
source_dropin=/lib/systemd/system/vonk-forge-package-helper.socket.d/20-package-upgrade-recovery.conf
repair_state=$source_state/repair
repair_phase=$repair_state/phase
repair_receipt=/var/lib/vonk-forge/package-repair.receipt
helper_receipt=/var/lib/vonk-forge/package-repair-helper.receipt
agent_unit=vonk-forge-agent.service
helper_unit=vonk-forge-package-helper.service
socket_unit=vonk-forge-package-helper.socket
recovery_unit=vonk-forge-package-upgrade-recover.service
firewall_name=vonk-forge-docker-firewall.service
lock_holder=
wrong_cgroup=
wrong_cgroup_pid=
sandbox_probe_unit=
native_transient_unit=
repair_probe_control=vonk-repair-helper.probe
synthetic_dpkg_fault_applied=false
repair_failed_line=unavailable
repair_failed_status=unavailable

trap 'repair_failed_status=$?; repair_failed_line=$LINENO' ERR

assert_repair_probe_not_persisted() {
  test ! -e "/var/lib/dpkg/tmp.ci/$repair_probe_control"
  test ! -L "/var/lib/dpkg/tmp.ci/$repair_probe_control"
  test -z "$(find /var/lib/dpkg/info -maxdepth 1 \
    -name "*$repair_probe_control*" -print -quit)"
}

dump_diagnostics() {
  printf '%s\n' '--- repair lifecycle diagnostics ---' >&2
  printf 'fixture: fault=%s crash_phase=%s standard_residue=%s\n' \
    "$fault" "$crash_phase" "$standard_residue" >&2
  printf 'failed assertion: line=%s status=%s expected-agent=%s:%s\n' \
    "$repair_failed_line" "$repair_failed_status" \
    "${agent_uid:-unset}" "${agent_gid:-unset}" >&2
  for receipt_path in "$repair_receipt" "$helper_receipt"; do
    printf '%s\n' "--- $receipt_path ---" >&2
    if [[ -f "$receipt_path" && ! -L "$receipt_path" ]]; then
      stat -c '%u:%g:%a:%h %s' "$receipt_path" >&2 || true
      sed -E 's/^repair_nonce=.*/repair_nonce=<redacted>/' \
        "$receipt_path" >&2 || true
    else
      printf '%s\n' '<absent-or-unsafe>' >&2
    fi
  done
  systemctl --system --no-pager --full status \
    "$agent_unit" "$helper_unit" "$socket_unit" "$recovery_unit" >&2 || true
  journalctl --system --no-pager -n 240 \
    -u "$agent_unit" -u "$helper_unit" -u "$recovery_unit" >&2 || true
  dpkg-query -W -f='${db:Status-Abbrev} ${Version}\n' vonk-forge-agent \
    >&2 || true
  dpkg --version | sed -n '1p' >&2 || true
  printf '%s\n' '--- dpkg transition journal ---' >&2
  find /var/lib/dpkg/updates -mindepth 1 -maxdepth 1 \
    -printf '%f %u:%g %m %n %s\n' | LC_ALL=C sort >&2 || true
  for database_path in /var/lib/dpkg/status /var/lib/dpkg/status-old; do
    printf '%s\n' "--- $database_path package record ---" >&2
    awk -v package=vonk-forge-agent '
      BEGIN { RS="" }
      $0 ~ "(^|\\n)Package: " package "(\\n|$)" { print; print "" }
    ' "$database_path" >&2 || true
  done
  for update_path in /var/lib/dpkg/updates/*; do
    [[ -f "$update_path" ]] || continue
    printf '%s\n' "--- $update_path ---" >&2
    sed -n '1,120p' "$update_path" >&2 || true
  done
  find /var/lib/vonk-forge/package-upgrade -maxdepth 3 -type f \
    ! -name '*.deb' -print \
    -exec sed -E \
      's/^(repair_nonce|recovery_nonce|source_recovery_nonce)=.*/\1=<redacted>/' \
      {} \; \
    >&2 2>/dev/null || true
  find /var/lib/vonk-forge/package-upgrade -maxdepth 3 -type f \
    -name '*.deb' -print -exec sha256sum {} \; >&2 2>/dev/null || true
}

cleanup() {
  status=$?
  (( status == 0 )) || dump_diagnostics
  if [[ -n "$lock_holder" ]]; then
    kill "$lock_holder" >/dev/null 2>&1 || true
    wait "$lock_holder" >/dev/null 2>&1 || true
  fi
  systemctl --system stop "$recovery_unit" "$agent_unit" "$helper_unit" \
    "$socket_unit" "$firewall_name" >/dev/null 2>&1 || true
  if [[ -n "$sandbox_probe_unit" ]]; then
    systemctl --system stop "$sandbox_probe_unit" >/dev/null 2>&1 || true
    systemctl --system reset-failed "$sandbox_probe_unit" >/dev/null 2>&1 || true
  fi
  if [[ -n "$native_transient_unit" ]]; then
    systemctl --system stop "$native_transient_unit" >/dev/null 2>&1 || true
    systemctl --system reset-failed "$native_transient_unit" \
      >/dev/null 2>&1 || true
  fi
  rm -f -- "/var/lib/dpkg/tmp.ci/$repair_probe_control" \
    /var/lib/dpkg/tmp.ci/vonk-repair-probe-sandbox-denials
  systemctl --system reset-failed "$recovery_unit" "$agent_unit" \
    "$helper_unit" "$socket_unit" "$firewall_name" >/dev/null 2>&1 || true
  if [[ -n "$wrong_cgroup" ]]; then
    if [[ -n "$wrong_cgroup_pid" ]] \
      && grep -Fxq "0::/${wrong_cgroup#/sys/fs/cgroup/}" \
        "/proc/$wrong_cgroup_pid/cgroup" 2>/dev/null; then
      kill "$wrong_cgroup_pid" >/dev/null 2>&1 || true
      for _ in {1..100}; do
        grep -Fxq "0::/${wrong_cgroup#/sys/fs/cgroup/}" \
          "/proc/$wrong_cgroup_pid/cgroup" 2>/dev/null || break
        sleep 0.01
      done
      if grep -Fxq "0::/${wrong_cgroup#/sys/fs/cgroup/}" \
        "/proc/$wrong_cgroup_pid/cgroup" 2>/dev/null; then
        kill -KILL "$wrong_cgroup_pid" >/dev/null 2>&1 || true
      fi
      for _ in {1..100}; do
        grep -Fxq "0::/${wrong_cgroup#/sys/fs/cgroup/}" \
          "/proc/$wrong_cgroup_pid/cgroup" 2>/dev/null || break
        sleep 0.01
      done
    fi
    rmdir -- "$wrong_cgroup" >/dev/null 2>&1 || true
  fi
  rm -f -- "$trigger" "$trigger_stage" "$result" "$started" "$firewall_unit"
  rm -rf -- /var/lib/vonk-forge/package-upgrade
  rm -f -- /var/lib/vonk-forge/helper-upgrade.pending \
    /var/lib/vonk-forge/helper-upgrade.receipt \
    /var/lib/vonk-forge/package-repair.receipt \
    /var/lib/vonk-forge/package-repair-helper.receipt
  rm -rf -- /run/vonk-forge-package-candidates
  rm -rf -- /lib/systemd/system/vonk-forge-package-helper.socket.d
  rm -rf -- /lib/systemd/system/vonk-forge-package-helper.service.d
  rm -rf -- /run/vonk-forge-package-helper
  rm -f -- "$standard_runner"
  case "$fault" in
    dpkg-iU) fixture_dpkg_status='iU '; fixture_dpkg_version=$installed_version ;;
    dpkg-iF) fixture_dpkg_status='ii '; fixture_dpkg_version=$installed_version ;;
    dpkg-iHR) fixture_dpkg_status='iH '; fixture_dpkg_version=$installed_version ;;
    absent) fixture_dpkg_status='ic '; fixture_dpkg_version=$installed_version ;;
    newer) fixture_dpkg_status='ii '; fixture_dpkg_version=$ordinary_version ;;
    *) fixture_dpkg_status=; fixture_dpkg_version= ;;
  esac
  if [[ "$synthetic_dpkg_fault_applied" = true ]]; then
    if [[ "$(dpkg-query -W -f='${db:Status-Abbrev}' \
      vonk-forge-agent 2>/dev/null)" != "$fixture_dpkg_status" \
      || "$(dpkg-query -W -f='${Version}' \
        vonk-forge-agent 2>/dev/null)" != "$fixture_dpkg_version" ]]; then
      printf 'unexpected synthetic dpkg cleanup state for %s\n' "$fault" >&2
      status=1
    fi
    cleanup_normalize_log=$test_root/cleanup-normalize.log
    if [[ "$fault" = newer ]]; then
      force_dpkg_version "$installed_version" || status=1
    fi
    if ! SYSTEMD_OFFLINE=1 dpkg --install --force-confold --force-downgrade \
      "$old_package" >"$cleanup_normalize_log" 2>&1; then
      printf 'failed to normalize synthetic dpkg cleanup state for %s\n' \
        "$fault" >&2
      sed -n '1,160p' "$cleanup_normalize_log" >&2 || true
      status=1
    elif [[ "$(dpkg-query -W \
      -f='${db:Status-Abbrev}|${Architecture}|${Version}' \
      vonk-forge-agent 2>/dev/null)" \
      != "ii |arm64|$installed_version" ]]; then
      printf 'normalized synthetic dpkg cleanup state is not exact for %s\n' \
        "$fault" >&2
      status=1
    fi
  fi
  if dpkg-query --show vonk-forge-agent >/dev/null 2>&1; then
    cleanup_dpkg_log=$test_root/cleanup-dpkg.log
    if ! SYSTEMD_OFFLINE=1 dpkg --purge --force-remove-reinstreq \
      vonk-forge-agent >"$cleanup_dpkg_log" 2>&1; then
      sed -n '1,160p' "$cleanup_dpkg_log" >&2 || true
      status=1
    fi
  fi
  systemctl --system daemon-reload >/dev/null 2>&1 || true
  rm -rf -- /etc/vonk-forge-agent
  rm -rf -- /var/lib/vonk-forge-agent
  rm -rf -- /var/lib/vonk-forge
  if [[ -e /var/lib/systemd/linger/vonk-agent ]]; then
    loginctl disable-linger vonk-agent >/dev/null 2>&1 \
      || rm -f -- /var/lib/systemd/linger/vonk-agent
  fi
  if getent passwd vonk-agent >/dev/null 2>&1; then
    userdel vonk-agent >/dev/null 2>&1 || status=1
  fi
  if getent group vonk-agent >/dev/null 2>&1; then
    groupdel vonk-agent >/dev/null 2>&1 || status=1
  fi
  rm -rf -- "$test_root"
  trap - EXIT
  exit "$status"
}

probe_info_collision="$(find /var/lib/dpkg/info -maxdepth 1 \
  -name "*$repair_probe_control*" -print -quit)"
if dpkg-query -W vonk-forge-agent >/dev/null 2>&1 \
  || systemctl --system cat "$agent_unit" >/dev/null 2>&1 \
  || systemctl --system cat "$helper_unit" >/dev/null 2>&1 \
  || systemctl --system cat "$socket_unit" >/dev/null 2>&1 \
  || systemctl --system cat "$recovery_unit" >/dev/null 2>&1 \
  || systemctl --system cat "$firewall_name" >/dev/null 2>&1 \
  || getent passwd vonk-agent >/dev/null 2>&1 \
  || getent group vonk-agent >/dev/null 2>&1 \
  || grep -Eq '^vonk-agent:' /etc/subuid /etc/subgid 2>/dev/null \
  || [[ -e /var/lib/vonk-forge \
    || -L /var/lib/vonk-forge \
    || -e /var/lib/systemd/linger/vonk-agent \
    || -L /var/lib/systemd/linger/vonk-agent \
    || -e /etc/vonk-forge-agent \
    || -L /etc/vonk-forge-agent \
    || -e /var/lib/vonk-forge-agent \
    || -L /var/lib/vonk-forge-agent \
    || -e /var/lib/vonk-forge/incoming \
    || -L /var/lib/vonk-forge/incoming \
    || -e /usr/lib/vonk-forge \
    || -L /usr/lib/vonk-forge \
    || -e /usr/share/doc/vonk-forge-agent \
    || -L /usr/share/doc/vonk-forge-agent \
    || -e /usr/share/keyrings/vonk-forge-release.pub \
    || -L /usr/share/keyrings/vonk-forge-release.pub \
    || -e /lib/systemd/system/vonk-forge-package-helper.service.d \
    || -L /lib/systemd/system/vonk-forge-package-helper.service.d \
    || -e "$source_gate" \
    || -L "$source_gate" \
    || -e /lib/systemd/system/vonk-forge-package-helper.socket.d \
    || -L /lib/systemd/system/vonk-forge-package-helper.socket.d \
    || -e /run/vonk-forge-package-candidates \
    || -L /run/vonk-forge-package-candidates \
    || -e /run/vonk-forge-package-helper \
    || -L /run/vonk-forge-package-helper \
    || -e "/var/lib/dpkg/tmp.ci/$repair_probe_control" \
    || -L "/var/lib/dpkg/tmp.ci/$repair_probe_control" \
    || -n "$probe_info_collision" \
    || -e "$firewall_unit" \
    || -L "$firewall_unit" ]]; then
  printf '%s\n' 'agent repair fixture would collide with host state' >&2
  exit 1
fi

test_root="$(mktemp -d /var/lib/vonk-forge-repair-test.XXXXXX)"
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$test_root"/{old,target,next}-{bin,dist} "$test_root/extracted"
build_digest_old="sha256:$(printf old-dev335 | sha256sum | cut -d' ' -f1)"
build_digest_target="sha256:$(printf exact-a122 | sha256sum | cut -d' ' -f1)"
build_digest_next="sha256:$(printf ordinary-next | sha256sum | cut -d' ' -f1)"

build_agent() {
  output=$1
  semantic=$2
  build_digest=$3
  identity=$4
  cat > "$test_root/agent-$identity.c" <<SOURCE
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static volatile const char build[] = "VONK_AGENT_BUILD_DIGEST=$build_digest";
static volatile const char semantic[] = "VONK_AGENT_SEMANTIC_VERSION=$semantic";
static volatile const char identity[] = "$identity";
int main(int argc, char **argv) {
  if (argc == 2 && strcmp(argv[1], "--version") == 0) {
    printf("vonk-agent %s\\n", "$semantic");
    return build[0] == 'V' && semantic[0] == 'V' && identity[0] != 0 ? 0 : 1;
  }
  if (argc >= 4 && strcmp(argv[1], "--config") == 0
      && strcmp(argv[3], "self-test") == 0) {
    char binary_digest[65] = {0};
    char digest_command[128] = {0};
    int command_length = snprintf(digest_command, sizeof digest_command,
        "/usr/bin/sha256sum /proc/%ld/exe", (long)getpid());
    if (command_length <= 0 || (size_t)command_length >= sizeof digest_command)
      return 2;
    FILE *digest = popen(digest_command, "r");
    if (!digest || fscanf(digest, "%64[0-9a-f]", binary_digest) != 1
        || pclose(digest) != 0 || strlen(binary_digest) != 64) return 2;
    printf("{\"semantic_version\":\"%s\",\"build_digest\":\"%s\",\"binary_digest\":\"%s\",\"architecture\":\"linux-arm64\",\"self_test_passed\":true}\\n", "$semantic", "$build_digest", binary_digest);
    return 0;
  }
  for (;;) pause();
}
SOURCE
  gcc -O2 -o "$output" "$test_root/agent-$identity.c"
}

build_helper() {
  output=$1
  identity=$2
  cat > "$test_root/helper-$identity.c" <<'SOURCE'
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
static volatile const char identity[] = "@IDENTITY@";
int main(void) {
  const char *trigger = "/var/lib/vonk-forge/test-helper-install.trigger";
  const char *result = "/var/lib/vonk-forge/test-helper-install.result";
  const char *started = "/var/lib/vonk-forge/test-helper-install.started";
  for (;;) {
    FILE *stream = fopen(trigger, "r");
    if (!stream) { usleep(20000); continue; }
    char package[4096];
    if (!fgets(package, sizeof package, stream)) { fclose(stream); return 2; }
    fclose(stream);
    package[strcspn(package, "\r\n")] = 0;
    unlink(trigger);
    FILE *begun = fopen(started, "w");
    if (!begun) return 4;
    fprintf(begun, "%c\n", identity[0]);
    fflush(begun);
    fsync(fileno(begun));
    fclose(begun);
    pid_t child = fork();
    if (child == 0) {
      execl("/usr/bin/dpkg", "/usr/bin/dpkg", "--install", "--force-confold",
            package, (char *)0);
      _exit(127);
    }
    if (child < 0) return 3;
    int status = 0;
    while (waitpid(child, &status, 0) < 0 && errno == EINTR) {}
    FILE *out = fopen(result, "w");
    if (out) {
      fprintf(out, "%d %c\n", WIFEXITED(status) ? WEXITSTATUS(status) : 255,
              identity[0]);
      fflush(out);
      fsync(fileno(out));
      fclose(out);
    }
  }
}
SOURCE
  sed -i "s/@IDENTITY@/$identity/" "$test_root/helper-$identity.c"
  gcc -O2 -o "$output" "$test_root/helper-$identity.c"
}

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

for generation in old target next; do
  case "$generation" in
    old) digest=$build_digest_old; marker=old; semantic=0.1.0 ;;
    target) digest=$build_digest_target; marker=target; semantic=0.1.0 ;;
    next) digest=$build_digest_next; marker=next; semantic=0.1.0 ;;
  esac
  build_agent "$test_root/$generation-bin/vonk-agent" "$semantic" "$digest" "$marker"
  build_helper "$test_root/$generation-bin/vonk-agent-helper" "$marker"
  cp -- "$test_root/$generation-bin/vonk-agent-helper" \
    "$test_root/$generation-bin/oras"
  cp -- "$build_egress_fixture" \
    "$test_root/$generation-bin/vonk-build-egress"
  printf '%s fixture license\n' "$generation" \
    > "$test_root/$generation-bin/oras.LICENSE"
  chmod 0555 \
    "$test_root/$generation-bin/"{vonk-agent,vonk-agent-helper,vonk-build-egress,oras}
  fixture_agent=$test_root/$generation-bin/vonk-agent
  fixture_agent_sha=$(sha256sum "$fixture_agent" | cut -d' ' -f1)
  fixture_self_test=$("$fixture_agent" --config /dev/null self-test)
  grep -Fq '"binary_digest":"'"$fixture_agent_sha"'"' \
    <<< "$fixture_self_test"
done

openssl genpkey -algorithm ED25519 -out "$test_root/release.pem"
chmod 0600 "$test_root/release.pem"
if [[ -n "${REPAIR_PROBE_BINARY:-}" ]]; then
  repair_probe_binary=$(realpath -e -- "$REPAIR_PROBE_BINARY")
  test "$repair_probe_binary" = \
    "$repo_root/target/release/vonk-repair-helper-probe"
else
  cargo build --locked --release --manifest-path "$repo_root/Cargo.toml" \
    --package vonk-repair-helper-probe
  repair_probe_binary=$repo_root/target/release/vonk-repair-helper-probe
fi
test ! -L "$repair_probe_binary"
test -f "$repair_probe_binary"
test "$(stat -c %u:%g:%a:%h "$repair_probe_binary")" = '0:0:755:1'

cat > "$test_root/probe-sandbox-denials.c" <<'SOURCE'
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/ptrace.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <unistd.h>

static int denied(long result, const char *name) {
  if (result != -1 || errno != EPERM) {
    fprintf(stderr, "%s was not denied with EPERM: result=%ld errno=%d\n",
            name, result, errno);
    return 1;
  }
  return 0;
}

int main(int argc, char **argv) {
  if (argc != 4
      || (strcmp(argv[1], "ptrace") != 0 && strcmp(argv[1], "zero") != 0)) {
    return 1;
  }
  char status[16384] = {0};
  char uid[128] = {0};
  char gid[128] = {0};
  if (snprintf(uid, sizeof(uid), "Uid:\t%s\t%s\t%s\t%s\n",
               argv[2], argv[2], argv[2], argv[2]) <= 0
      || snprintf(gid, sizeof(gid), "Gid:\t%s\t%s\t%s\t%s\n",
                  argv[3], argv[3], argv[3], argv[3]) <= 0) return 2;
  int fd = open("/proc/self/status", O_RDONLY | O_CLOEXEC);
  if (fd < 0) return 3;
  ssize_t count = read(fd, status, sizeof(status) - 1);
  if (count <= 0 || close(fd) != 0) return 4;
  if (!strstr(status, uid)
      || !strstr(status, gid)
      || !strstr(status, "CapInh:\t0000000000000000\n")
      || !strstr(status, "CapAmb:\t0000000000000000\n")
      || !strstr(status, "NoNewPrivs:\t1\n")
      || !strstr(status, "Seccomp:\t2\n")) return 5;
  const char *caps = strcmp(argv[1], "ptrace") == 0
    ? "0000000000080000" : "0000000000000000";
  char permitted[64] = {0};
  char effective[64] = {0};
  char bounding[64] = {0};
  if (snprintf(permitted, sizeof(permitted), "CapPrm:\t%s\n", caps) <= 0
      || snprintf(effective, sizeof(effective), "CapEff:\t%s\n", caps) <= 0
      || snprintf(bounding, sizeof(bounding), "CapBnd:\t%s\n", caps) <= 0
      || !strstr(status, permitted)
      || !strstr(status, effective)
      || !strstr(status, bounding)) return 6;

  errno = 0;
  if (denied(syscall(SYS_ptrace, PTRACE_TRACEME, 0, 0, 0), "ptrace")) return 7;
  struct iovec local = {.iov_base = status, .iov_len = 1};
  struct iovec remote = {.iov_base = status, .iov_len = 1};
  errno = 0;
  if (denied(syscall(SYS_process_vm_readv, getpid(), &local, 1, &remote, 1, 0),
             "process_vm_readv")) return 8;
  errno = 0;
  if (denied(syscall(SYS_process_vm_writev, getpid(), &local, 1, &remote, 1, 0),
             "process_vm_writev")) return 9;
  errno = 0;
  if (denied(syscall(SYS_pidfd_getfd, -1, -1, 0), "pidfd_getfd")) return 10;
  errno = 0;
  if (denied(syscall(SYS_kcmp, getpid(), getpid(), 0, 0, 0), "kcmp")) return 11;
  errno = 0;
  if (denied(syscall(SYS_socket, AF_INET, SOCK_STREAM, 0), "socket")) return 12;
  errno = 0;
  if (denied(syscall(SYS_mount, "none", "/", "none", 0, 0), "mount")) return 13;
  puts("probe sandbox denied syscalls: PASS");
  return 0;
}
SOURCE
gcc -O2 -Wall -Wextra -Werror -o "$test_root/probe-sandbox-denials" \
  "$test_root/probe-sandbox-denials.c"
chown root:root "$test_root/probe-sandbox-denials"
chmod 0755 "$test_root/probe-sandbox-denials"
test "$(stat -c %u:%g:%a:%h "$test_root/probe-sandbox-denials")" = 0:0:755:1

sandbox_probe_unit=vonk-repair-probe-sandbox-denials.service
test "$(systemctl --system show --property=LoadState --value \
  "$sandbox_probe_unit")" = not-found
sandbox_denial_output=$(/usr/bin/systemd-run --system --wait --pipe --collect --quiet \
  --service-type=exec --unit="$sandbox_probe_unit" \
  --property=User=root --property=Group=root \
  --property=NoNewPrivileges=yes \
  --property=CapabilityBoundingSet=CAP_SYS_PTRACE \
  --property=AmbientCapabilities= \
  --property=Environment=LANG=C --property=Environment=LC_ALL=C \
  --property=Environment=PATH=/usr/bin:/bin \
  --property=UnsetEnvironment=LD_PRELOAD \
  --property=UnsetEnvironment=LD_LIBRARY_PATH \
  --property=UnsetEnvironment=LD_AUDIT \
  --property=UnsetEnvironment=LD_DEBUG \
  --property=UnsetEnvironment=BASH_ENV \
  --property=UnsetEnvironment=ENV \
  --property=UnsetEnvironment=GCONV_PATH \
  --property=PrivateNetwork=yes --property=IPAddressDeny=any \
  --property=PrivateDevices=yes --property=DevicePolicy=closed \
  --property=ProtectSystem=strict --property=ProtectHome=yes \
  --property=ReadOnlyPaths=/ \
  --property=ProtectKernelTunables=yes \
  --property=ProtectKernelModules=yes \
  --property=ProtectKernelLogs=yes \
  --property=ProtectControlGroups=yes \
  --property=ProtectClock=yes --property=ProtectHostname=yes \
  --property=ProtectProc=default --property=ProcSubset=all \
  --property=RestrictSUIDSGID=yes --property=RestrictRealtime=yes \
  --property=RestrictNamespaces=yes --property=LockPersonality=yes \
  --property=MemoryDenyWriteExecute=yes --property=RemoveIPC=yes \
  --property=KeyringMode=private \
  --property=SystemCallArchitectures=native \
  --property=SystemCallFilter=@system-service \
  '--property=SystemCallFilter=~@network-io @mount @reboot @swap @obsolete @raw-io @resources @cpu-emulation @debug ptrace process_vm_readv process_vm_writev pidfd_getfd kcmp' \
  --property=SystemCallErrorNumber=EPERM \
  --property=RuntimeMaxSec=5s --property=TimeoutStartSec=5s \
  --property=TimeoutStopSec=1s --property=Restart=no \
  --property=UMask=0077 -- \
  /usr/bin/setpriv --no-new-privs -- \
    "$test_root/probe-sandbox-denials" ptrace 0 0)
test "$sandbox_denial_output" = 'probe sandbox denied syscalls: PASS'
for _ in {1..200}; do
  [[ "$(systemctl --system show --property=LoadState --value \
    "$sandbox_probe_unit")" = not-found ]] && break
  sleep 0.01
done
test "$(systemctl --system show --property=LoadState --value \
  "$sandbox_probe_unit")" = not-found
test ! -e "/run/systemd/transient/$sandbox_probe_unit"
sandbox_probe_unit=

build_package() {
  version=$1
  generation=$2
  digest=$3
  output=$4
  shift 4
  VONK_SOURCE_REVISION="$packaging_revision" \
  VONK_SOURCE_REPOSITORY=https://github.com/CarstVaartjes/vonk-forge \
    "$repo_root/scripts/build-agent-deb" \
      --version "$version" \
      --architecture linux-arm64 \
      --build-digest "$digest" \
      --release-private-key "$test_root/release.pem" \
      --binaries-dir "$test_root/$generation-bin" \
      --source-date-epoch "$epoch" \
      --output-dir "$output" "$@" >/dev/null
}

build_package "$installed_version" old "$build_digest_old" "$test_root/old-dist"
build_package "$source_version" target "$build_digest_target" "$test_root/target-dist"
old_package="$test_root/old-dist/vonk-forge-agent_${installed_version}_arm64.deb"
source_package="$test_root/target-dist/vonk-forge-agent_${source_version}_arm64.deb"
"$repo_root/scripts/verify-agent-deb" --json "$old_package" >/dev/null
"$repo_root/scripts/verify-agent-deb" --json "$source_package" >/dev/null
for ordinary_package in "$old_package" "$source_package"; do
  ordinary_name=$(basename "$ordinary_package" .deb)
  ordinary_payload=$test_root/extracted/$ordinary_name
  dpkg-deb --extract "$ordinary_package" "$ordinary_payload"
  ordinary_egress=$ordinary_payload/usr/lib/vonk-forge/vonk-build-egress
  test -f "$ordinary_egress"
  test ! -L "$ordinary_egress"
  test "$(stat -c %u:%g:%a:%h "$ordinary_egress")" = 0:0:555:1
  cmp -s -- "$build_egress_fixture" "$ordinary_egress"
done

cat > "$firewall_unit" <<'UNIT'
[Unit]
Description=Vonk Forge repair firewall fixture
[Service]
Type=oneshot
ExecStart=/bin/true
RemainAfterExit=yes
UNIT
chmod 0644 "$firewall_unit"
systemctl --system daemon-reload
systemctl --system start "$firewall_name"

SYSTEMD_OFFLINE=1 dpkg --install --force-confold "$old_package"
test "$(dpkg-query -W -f='${db:Status-Abbrev}' vonk-forge-agent)" = 'ii '
assert_repair_probe_not_persisted

install -d -o root -g root -m 0755 /etc/vonk-forge-agent
printf 'node_id = "%s"\n' "$node_id" > /etc/vonk-forge-agent/agent.toml
chown root:root /etc/vonk-forge-agent/agent.toml
chmod 0644 /etc/vonk-forge-agent/agent.toml
install -d -o vonk-agent -g vonk-agent -m 0700 /var/lib/vonk-forge-agent
install -d -o root -g root -m 0755 /var/lib/vonk-forge
install -d -o vonk-agent -g vonk-agent -m 0700 /var/lib/vonk-forge/incoming
systemctl --system daemon-reload
systemctl --system enable --now "$socket_unit" >/dev/null
systemctl --system start "$helper_unit"
systemctl --system start "$agent_unit"

old_agent_pid="$(systemctl --system show --property=MainPID --value "$agent_unit")"
old_helper_pid="$(systemctl --system show --property=MainPID --value "$helper_unit")"
test "$old_agent_pid" -gt 1
test "$old_helper_pid" -gt 1

old_agent_sha="$(sha256sum /usr/lib/vonk-forge/vonk-agent | cut -d' ' -f1)"
old_helper_sha="$(sha256sum /usr/lib/vonk-forge/vonk-agent-helper | cut -d' ' -f1)"
old_agent_unit_sha="$(sha256sum /lib/systemd/system/vonk-forge-agent.service | cut -d' ' -f1)"
old_helper_unit_sha="$(sha256sum /lib/systemd/system/vonk-forge-package-helper.service | cut -d' ' -f1)"
old_socket_sha="$(sha256sum /lib/systemd/system/vonk-forge-package-helper.socket | cut -d' ' -f1)"

source_payload=$test_root/extracted/source-payload
dpkg-deb --extract "$source_package" "$source_payload"
source_package_sha="$(sha256sum "$source_package" | cut -d' ' -f1)"
source_package_bytes="$(stat -c %s "$source_package")"
source_agent_sha="$(sha256sum "$test_root/target-bin/vonk-agent" | cut -d' ' -f1)"
source_helper_sha="$(sha256sum "$test_root/target-bin/vonk-agent-helper" | cut -d' ' -f1)"
source_runner_file=$source_payload/usr/lib/vonk-forge/vonk-forge-package-upgrade-recover
source_unit_file=$source_payload/lib/systemd/system/vonk-forge-package-upgrade-recover.service
source_gate_file=$source_payload/lib/systemd/system/vonk-forge-agent.service.d/20-package-upgrade-recovery.conf
source_runner_sha="$(sha256sum "$source_runner_file" | cut -d' ' -f1)"
source_unit_sha="$(sha256sum "$source_unit_file" | cut -d' ' -f1)"
source_gate_sha="$(sha256sum "$source_gate_file" | cut -d' ' -f1)"

install -d -o root -g root -m 0700 "$source_state"
install -o root -g root -m 0600 /dev/null "$source_lock"
install -o root -g root -m 0600 "$source_package" \
  "$source_state/$source_package_sha.deb"
install -o root -g root -m 0555 "$source_runner_file" "$source_runner"
install -o root -g root -m 0644 "$source_unit_file" "$source_unit"
install -d -o root -g root -m 0755 "${source_gate%/*}" "${source_dropin%/*}"
install -o root -g root -m 0644 "$source_gate_file" "$source_gate"
printf '%s\n' '[Unit]' 'Wants=vonk-forge-package-upgrade-recover.service' \
  > "$source_dropin"
chown root:root "$source_dropin"
chmod 0644 "$source_dropin"
source_dropin_sha="$(sha256sum "$source_dropin" | cut -d' ' -f1)"
printf '%s\n' \
  'schema_version=1' \
  "target_version=$source_version" \
  "helper_sha256=$source_helper_sha" \
  "agent_sha256=$source_agent_sha" > "$source_blocker"
chown root:root "$source_blocker"
chmod 0600 "$source_blocker"
source_blocker_sha="$(sha256sum "$source_blocker" | cut -d' ' -f1)"
printf '%s\n' \
  "version=$source_version" \
  "helper_sha256=$source_helper_sha" \
  "agent_sha256=$source_agent_sha" > "$source_pending"
chown root:root "$source_pending"
chmod 0600 "$source_pending"
source_pending_sha="$(sha256sum "$source_pending" | cut -d' ' -f1)"
boot_id="$(sed -n '1p' /proc/sys/kernel/random/boot_id)"
source_nonce="$(openssl rand -hex 32)"
printf '%s\n' \
  'schema_version=1' \
  'package=vonk-forge-agent' \
  "target_version=$source_version" \
  'architecture=arm64' \
  "package_sha256=$source_package_sha" \
  "package_bytes=$source_package_bytes" \
  "agent_sha256=$source_agent_sha" \
  "helper_sha256=$source_helper_sha" \
  "runner_sha256=$source_runner_sha" \
  "unit_sha256=$source_unit_sha" \
  "request_boot_id=$boot_id" \
  'dpkg_pid=999999' \
  'dpkg_start_time=1' \
  "recovery_nonce=$source_nonce" > "$source_intent"
chown root:root "$source_intent"
chmod 0600 "$source_intent"
source_intent_sha="$(sha256sum "$source_intent" | cut -d' ' -f1)"

setpriv_sha="$(sha256sum /usr/bin/setpriv | cut -d' ' -f1)"
repair_probe_sha="$(sha256sum "$repair_probe_binary" | cut -d' ' -f1)"
mapfile -t util_linux_fields < <(LC_ALL=C dpkg-query -W \
  -f='${db:Status-Abbrev}\n${binary:Package}\n${Architecture}\n${Version}\n' \
  util-linux)
test "${#util_linux_fields[@]}" -eq 4
test "${util_linux_fields[0]}" = 'ii '
test "${util_linux_fields[1]}" = util-linux
test "${util_linux_fields[2]}" = arm64
util_linux_version=${util_linux_fields[3]}

authority=$test_root/repair-authority
printf '%s\n' \
  'schema_version=1' \
  "node_id=$node_id" \
  "installed_version=$installed_version" \
  "installed_agent_sha256=$old_agent_sha" \
  "installed_helper_sha256=$old_helper_sha" \
  "installed_agent_unit_sha256=$old_agent_unit_sha" \
  "installed_helper_unit_sha256=$old_helper_unit_sha" \
  "installed_helper_socket_sha256=$old_socket_sha" \
  "source_target_version=$source_version" \
  'source_architecture=arm64' \
  "source_package_sha256=$source_package_sha" \
  "source_agent_sha256=$source_agent_sha" \
  "source_helper_sha256=$source_helper_sha" \
  "source_runner_sha256=$source_runner_sha" \
  "source_unit_sha256=$source_unit_sha" \
  "source_agent_gate_sha256=$source_gate_sha" \
  "source_socket_dropin_sha256=$source_dropin_sha" \
  "source_blocker_sha256=$source_blocker_sha" \
  "source_pending_sha256=$source_pending_sha" \
  "repair_probe_sha256=$repair_probe_sha" \
  "setpriv_sha256=$setpriv_sha" \
  "util_linux_version=$util_linux_version" > "$authority"
chmod 0600 "$authority"
authority_sha="$(sha256sum "$authority" | cut -d' ' -f1)"

wait_for_transient_collection() {
  local unit=$1
  for _ in {1..200}; do
    [[ "$(systemctl --system show --property=LoadState --value "$unit")" \
      = not-found ]] && break
    sleep 0.01
  done
  test "$(systemctl --system show --property=LoadState --value "$unit")" \
    = not-found
  test ! -e "/run/systemd/transient/$unit"
}

run_zero_cap_agent_transient() {
  local unit=$1
  shift
  native_transient_unit=$unit
  set +e
  transient_output=$(/usr/bin/systemd-run --system --wait --pipe --collect --quiet \
    --service-type=exec --unit="$unit" \
    --property=User=vonk-agent --property=Group=vonk-agent \
    --property=SupplementaryGroups= \
    --property=NoNewPrivileges=yes \
    --property=CapabilityBoundingSet= \
    --property=AmbientCapabilities= \
    --property=Environment=LANG=C --property=Environment=LC_ALL=C \
    --property=Environment=PATH=/usr/bin:/bin \
    --property=UnsetEnvironment=LD_PRELOAD \
    --property=UnsetEnvironment=LD_LIBRARY_PATH \
    --property=UnsetEnvironment=LD_AUDIT \
    --property=UnsetEnvironment=LD_DEBUG \
    --property=UnsetEnvironment=BASH_ENV \
    --property=UnsetEnvironment=ENV \
    --property=UnsetEnvironment=GCONV_PATH \
    --property=PrivateNetwork=yes --property=IPAddressDeny=any \
    --property=PrivateDevices=yes --property=DevicePolicy=closed \
    --property=ProtectSystem=strict --property=ProtectHome=yes \
    --property=ReadOnlyPaths=/ \
    --property=ProtectKernelTunables=yes \
    --property=ProtectKernelModules=yes \
    --property=ProtectKernelLogs=yes \
    --property=ProtectControlGroups=yes \
    --property=ProtectClock=yes --property=ProtectHostname=yes \
    --property=ProtectProc=default --property=ProcSubset=all \
    --property=RestrictSUIDSGID=yes --property=RestrictRealtime=yes \
    --property=RestrictNamespaces=yes --property=LockPersonality=yes \
    --property=MemoryDenyWriteExecute=yes --property=RemoveIPC=yes \
    --property=KeyringMode=private \
    --property=SystemCallArchitectures=native \
    --property=SystemCallFilter=@system-service \
    '--property=SystemCallFilter=~@network-io @mount @reboot @swap @obsolete @raw-io @resources @cpu-emulation @debug ptrace process_vm_readv process_vm_writev pidfd_getfd kcmp' \
    --property=SystemCallErrorNumber=EPERM \
    --property=RuntimeMaxSec=5s --property=TimeoutStartSec=5s \
    --property=TimeoutStopSec=1s --property=Restart=no \
    --property=UMask=0077 -- "$@" 2>&1)
  transient_status=$?
  set -e
  wait_for_transient_collection "$unit"
  native_transient_unit=
}

agent_uid=$(id -u vonk-agent)
agent_gid=$(id -g vonk-agent)
test "$agent_uid" -gt 1
test "$agent_gid" -gt 1
agent_groups=$(awk '/^Groups:/ {
  if (NF == 1) {
    print "none"
    next
  }
  groups=$2
  for (group_index=3; group_index <= NF; group_index++) {
    groups=groups "," $group_index
  }
  print groups
}' "/proc/$old_agent_pid/status")
[[ "$agent_groups" = none || "$agent_groups" = "$agent_gid" ]]
agent_start=$(awk '{print $22}' "/proc/$old_agent_pid/stat")
agent_boot=$(sed -n '1p' /proc/sys/kernel/random/boot_id)
agent_invocation=$(systemctl --system show --property=InvocationID --value \
  "$agent_unit")
[[ "$agent_invocation" =~ ^[0-9a-f]{32}$ ]]
grep -E '^(Groups|CapInh|CapPrm|CapEff|CapBnd|CapAmb):' \
  "/proc/$old_agent_pid/status" | sed 's/^/agent-runtime /'
grep -Fxq $'CapInh:\t0000000000200000' "/proc/$old_agent_pid/status"
grep -Fxq $'CapPrm:\t0000000000000000' "/proc/$old_agent_pid/status"
grep -Fxq $'CapEff:\t0000000000000000' "/proc/$old_agent_pid/status"
grep -Fxq $'CapBnd:\t00000000002000c2' "/proc/$old_agent_pid/status"
grep -Fxq $'CapAmb:\t0000000000000000' "/proc/$old_agent_pid/status"

install -d -o root -g root -m 0755 /var/lib/dpkg/tmp.ci
native_probe=/var/lib/dpkg/tmp.ci/vonk-repair-helper.probe
native_denials=/var/lib/dpkg/tmp.ci/vonk-repair-probe-sandbox-denials
install -o root -g root -m 0755 "$repair_probe_binary" "$native_probe"
install -o root -g root -m 0755 "$test_root/probe-sandbox-denials" \
  "$native_denials"
test "$(stat -c %u:%g:%a:%h "$native_probe")" = 0:0:755:1
test "$(stat -c %u:%g:%a:%h "$native_denials")" = 0:0:755:1

native_nonce=$(openssl rand -hex 32)
run_zero_cap_agent_transient \
  "vonk-repair-agent-sandbox-${native_nonce}.service" \
  "$native_denials" zero "$agent_uid" "$agent_gid"
if [[ "$transient_status" -ne 0 ]]; then
  printf 'zero-cap sandbox failed: %s\n' "$transient_output" >&2
  exit 1
fi
test "$transient_output" = 'probe sandbox denied syscalls: PASS'

probe_args=(
  "$old_agent_pid" "$agent_start" "$native_nonce" "$authority_sha"
  "$old_agent_sha" "$agent_boot" "$agent_invocation" "$agent_uid"
  "$agent_gid" "$agent_groups" "$setpriv_sha" "$repair_probe_sha"
)
run_zero_cap_agent_transient \
  "vonk-repair-agent-positive-${native_nonce}.service" \
  "$native_probe" probe-agent "${probe_args[@]}"
if [[ "$transient_status" -ne 0 ]]; then
  printf 'agent identity probe failed: %s\n' "$transient_output" >&2
  exit 1
fi
grep -Eq \
  "^schema_version=1 nonce=$native_nonce authority_sha256=$authority_sha agent_pid=$old_agent_pid agent_start=$agent_start agent_sha256=$old_agent_sha boot_id=$agent_boot invocation_id=$agent_invocation agent_uid=$agent_uid agent_gid=$agent_gid agent_groups=$agent_groups exe_dev=[1-9][0-9]* exe_ino=[1-9][0-9]* cap_eff=0000000000000000 cap_ambient=0000000000000000 no_new_privs=1 seccomp=2$" \
  <<< "$transient_output"

wrong_boot=00000000-0000-0000-0000-000000000001
wrong_digest=$(printf '0%.0s' {1..64})
negative_cases=(
  "uid|7|0"
  "gid|8|0"
  "groups|9|0"
  "pid|0|$old_helper_pid"
  "start|1|1"
  "boot|5|$wrong_boot"
  "agent-digest|4|$wrong_digest"
  "setpriv-digest|10|$wrong_digest"
  "probe-digest|11|$wrong_digest"
)
for negative_case in "${negative_cases[@]}"; do
  IFS='|' read -r negative_label negative_index negative_value \
    <<< "$negative_case"
  negative_args=("${probe_args[@]}")
  negative_args[negative_index]=$negative_value
  negative_nonce=$(openssl rand -hex 32)
  negative_args[2]=$negative_nonce
  run_zero_cap_agent_transient \
    "vonk-repair-agent-negative-${negative_label}-${negative_nonce}.service" \
    "$native_probe" probe-agent "${negative_args[@]}"
  test "$transient_status" -ne 0
done

collision_nonce=$(openssl rand -hex 32)
collision_unit="vonk-repair-agent-collision-${collision_nonce}.service"
native_transient_unit=$collision_unit
/usr/bin/systemd-run --system --collect --quiet --unit="$collision_unit" \
  -- /bin/sleep 1
set +e
/usr/bin/systemd-run --system --wait --pipe --collect --quiet \
  --unit="$collision_unit" -- /bin/true >/dev/null 2>&1
collision_status=$?
set -e
test "$collision_status" -ne 0
wait_for_transient_collection "$collision_unit"
native_transient_unit=

rm -f -- "$native_denials" "$native_probe"
assert_repair_probe_not_persisted

assert_fixture_equal() {
  local label=$1
  local expected=$2
  local observed=$3
  if [[ "$observed" != "$expected" ]]; then
    printf 'old-package fixture mismatch: %s expected=%s observed=%s\n' \
      "$label" "$expected" "$observed" >&2
    exit 1
  fi
}

assert_old_fixture() {
  local fixture fixture_path fixture_mode fixture_sha fixture_label
  local -a installed_fields
  mapfile -t installed_fields < <(LC_ALL=C dpkg-query -W \
    -f='${db:Status-Abbrev}\n${binary:Package}\n${Architecture}\n${Version}\n' \
    vonk-forge-agent)
  assert_fixture_equal package-field-count 4 "${#installed_fields[@]}"
  assert_fixture_equal package-status 'ii ' "${installed_fields[0]}"
  assert_fixture_equal package-name vonk-forge-agent "${installed_fields[1]}"
  assert_fixture_equal package-architecture arm64 "${installed_fields[2]}"
  assert_fixture_equal host-architecture arm64 "$(dpkg --print-architecture)"
  assert_fixture_equal package-version "$installed_version" "${installed_fields[3]}"
  for fixture in \
    "/usr/lib/vonk-forge/vonk-agent|555|$old_agent_sha|agent" \
    "/usr/lib/vonk-forge/vonk-agent-helper|555|$old_helper_sha|helper" \
    "/lib/systemd/system/vonk-forge-agent.service|644|$old_agent_unit_sha|agent-unit" \
    "/lib/systemd/system/vonk-forge-package-helper.service|644|$old_helper_unit_sha|helper-unit" \
    "/lib/systemd/system/vonk-forge-package-helper.socket|644|$old_socket_sha|helper-socket"; do
    IFS='|' read -r fixture_path fixture_mode fixture_sha fixture_label \
      <<< "$fixture"
    assert_fixture_equal "$fixture_label-stat" "0:0:$fixture_mode:1" \
      "$(stat -c %u:%g:%a:%h "$fixture_path")"
    assert_fixture_equal "$fixture_label-sha256" "$fixture_sha" \
      "$(sha256sum "$fixture_path" | cut -d' ' -f1)"
  done
}

assert_old_fixture

VONK_REPAIR_BINARY_SOURCE_REVISION="$binary_revision" \
VONK_SOURCE_REVISION="$packaging_revision" \
VONK_SOURCE_REPOSITORY=https://github.com/CarstVaartjes/vonk-forge \
  "$repo_root/scripts/build-agent-deb" \
    --version "$repair_version" \
    --architecture linux-arm64 \
    --build-digest "$build_digest_target" \
    --release-private-key "$test_root/release.pem" \
    --binaries-dir "$test_root/target-bin" \
    --source-date-epoch "$epoch" \
    --repair-authority "$authority" \
    --repair-probe-binary "$repair_probe_binary" \
    --output-dir "$test_root/repair-dist" >/dev/null
repair_package=$test_root/repair-dist/vonk-forge-agent_${repair_version}_arm64.deb
repair_sha="$(sha256sum "$repair_package" | cut -d' ' -f1)"
repair_control=$test_root/extracted/repair-control
dpkg-deb --control "$repair_package" "$repair_control"
repair_runner_sha="$(sha256sum "$repair_control/preinst" | cut -d' ' -f1)"
repair_payload=$test_root/extracted/repair-payload
dpkg-deb --extract "$repair_package" "$repair_payload"
repair_standard_payload=$repair_payload/usr/lib/vonk-forge/\
vonk-forge-package-upgrade-recover.standard
test -f "$repair_standard_payload"
test ! -e "$repair_payload/usr/lib/vonk-forge/vonk-build-egress"
test ! -L "$repair_payload/usr/lib/vonk-forge/vonk-build-egress"
if [[ "$standard_residue" = exact-0755 ]]; then
  install -o root -g root -m 0755 "$repair_standard_payload" "$standard_runner"
  test "$(stat -c %u:%g:%a:%h "$standard_runner")" = 0:0:755:1
fi
release_key_sha="$(/usr/bin/python3 -c \
  'import hashlib, pathlib, sys; print(hashlib.sha256(bytes.fromhex(pathlib.Path(sys.argv[1]).read_text().strip())).hexdigest())' \
  "$source_payload/usr/share/keyrings/vonk-forge-release.pub")"
"$repo_root/scripts/verify-agent-deb" --repair --json \
  --expected-node-id "$node_id" \
  --expected-repair-authority-sha256 "$authority_sha" \
  --expected-release-key-sha256 "$release_key_sha" \
  --expected-binary-source-revision "$binary_revision" \
  --expected-packaging-source-revision "$packaging_revision" \
  "$repair_package"

snapshot_state() {
  destination=$1
  {
    dpkg-query -W -f='dpkg=${db:Status-Abbrev}|${Architecture}|${Version}\n' \
      vonk-forge-agent 2>/dev/null || printf 'dpkg=absent\n'
    for unit in "$agent_unit" "$helper_unit" "$recovery_unit"; do
      systemctl --system show --property=LoadState --property=ActiveState \
        --property=SubState --property=MainPID --property=InvocationID \
        --property=ControlGroup --property=DropInPaths \
        --property=ExecCondition "$unit" | sed "s|^|unit=$unit |"
      pid="$(systemctl --system show --property=MainPID --value "$unit")"
      if [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 && -r "/proc/$pid/stat" ]]; then
        printf 'pid=%s start=%s exe=%s sha=%s cgroup=%s\n' \
          "$pid" "$(awk '{print $22}' "/proc/$pid/stat")" \
          "$(readlink "/proc/$pid/exe")" \
          "$(sha256sum "/proc/$pid/exe" | cut -d' ' -f1)" \
          "$(tr '\n' ',' < "/proc/$pid/cgroup")"
      fi
    done
    paths=(
      /usr/lib/vonk-forge/vonk-agent
      /usr/lib/vonk-forge/vonk-agent-helper
      /lib/systemd/system/vonk-forge-agent.service
      /lib/systemd/system/vonk-forge-package-helper.service
      /lib/systemd/system/vonk-forge-package-helper.socket
      /etc/vonk-forge-agent/agent.toml
      "$source_intent" "$source_lock" "$source_blocker" "$source_pending"
      "$source_state/$source_package_sha.deb"
      "$source_runner" "$source_unit" "$source_gate" "$source_dropin"
      "$standard_runner" "${standard_runner}.new"
      "$repair_receipt" "$helper_receipt"
      "$custody/$repair_sha.deb"
    )
    for path in "${paths[@]}"; do
      if [[ -L "$path" ]]; then
        printf 'path=%s lstat=%s symlink=%s\n' "$path" \
          "$(stat -c %d:%i:%u:%g:%a:%h:%s:%Y:%Z "$path")" \
          "$(readlink "$path")"
      elif [[ -f "$path" ]]; then
        printf 'path=%s stat=%s sha=%s\n' "$path" \
          "$(stat -c %d:%i:%u:%g:%a:%h:%s:%Y:%Z "$path")" \
          "$(sha256sum "$path" | cut -d' ' -f1)"
      else
        printf 'path=%s absent\n' "$path"
      fi
    done
    if [[ -d "$repair_state" ]]; then
      printf 'repair-dir=%s\n' \
        "$(stat -c %d:%i:%u:%g:%a:%h:%s:%Y:%Z "$repair_state")"
      find "$repair_state" -mindepth 1 -maxdepth 1 \
        -printf 'repair=%f %y %D:%i:%u:%g:%m:%n:%s:%T@:%C@\n' \
        | sort
    else
      printf 'repair=absent\n'
    fi
  } > "$destination"
}

assert_exact_dpkg_transition() {
  expected_before=$1
  expected_after=$2
  grep -Fxq "dpkg=$expected_before" "$test_root/before"
  grep -Fxq "dpkg=$expected_after" "$test_root/after"
  test -z "$(find /var/lib/dpkg/updates -mindepth 1 -maxdepth 1 \
    -print -quit)"
  cmp -s "$test_root/before-dpkg-status" /var/lib/dpkg/status-old
  expected_abbrev=${expected_after%%|*}
  case "$expected_abbrev" in
    'iU ') expected_status='install ok unpacked' ;;
    'ii ') expected_status='install ok installed' ;;
    'iH ') expected_status='install ok half-installed' ;;
    'ic ') expected_status='install ok config-files' ;;
    *) printf 'unsupported expected dpkg status: %s\n' \
      "$expected_abbrev" >&2; return 1 ;;
  esac
  awk -v replacement="$expected_status" '
    BEGIN { RS=""; ORS="\n\n" }
    $0 ~ /(^|\n)Package: vonk-forge-agent(\n|$)/ {
      sub(/Status: [^\n]+/, "Status: " replacement)
    }
    { print }
  ' "$test_root/before-dpkg-status" > "$test_root/expected-dpkg-status"
  cmp -s "$test_root/expected-dpkg-status" /var/lib/dpkg/status
  sed '/^dpkg=/d' "$test_root/before" > "$test_root/before-without-dpkg"
  sed '/^dpkg=/d' "$test_root/after" > "$test_root/after-without-dpkg"
  cmp -s "$test_root/before-without-dpkg" "$test_root/after-without-dpkg"
}

snapshot_source_authority_state() {
  authority_destination=$1
  authority_dpkg_status=$2
  authority_full_snapshot=${authority_destination}.full
  snapshot_state "$authority_full_snapshot"
  grep -Fxq \
    "dpkg=$authority_dpkg_status|arm64|$installed_version" \
    "$authority_full_snapshot"
  grep -Fv -e "path=$standard_runner " \
    -e "path=${standard_runner}.new " "$authority_full_snapshot" \
    | sed '/^dpkg=/d' \
    > "$authority_destination"
  rm -f -- "$authority_full_snapshot"
}

snapshot_prepared_objects() {
  destination=$1
  {
    test -f "$standard_runner"
    printf 'standard=%s sha=%s\n' \
      "$(stat -c %d:%i:%u:%g:%a:%h:%s:%Y:%Z "$standard_runner")" \
      "$(sha256sum "$standard_runner" | cut -d' ' -f1)"
    find "$source_state" -mindepth 1 -maxdepth 1 -type d \
      -name '.repair-build.*' -print | sort
    while IFS= read -r prepared; do
      printf 'prepared=%s stat=%s' "$prepared" \
        "$(stat -c %d:%i:%u:%g:%a:%h:%s:%Y:%Z "$prepared")"
      if [[ -f "$prepared" ]]; then
        printf ' sha=%s' "$(sha256sum "$prepared" | cut -d' ' -f1)"
      elif [[ -L "$prepared" ]]; then
        printf ' link=%s' "$(readlink "$prepared")"
      fi
      printf '\n'
    done < <(find "$source_state" -mindepth 2 -maxdepth 2 \
      -path "$source_state/.repair-build.*/*" -print | sort)
  } > "$destination"
}

submit_helper_install() {
  package_path=$1
  printf '%s\n' "$package_path" > "$trigger_stage"
  chown root:root "$trigger_stage"
  chmod 0600 "$trigger_stage"
  mv -f -- "$trigger_stage" "$trigger"
}

stage_helper_candidate() {
  source_path=$1
  candidate_name=${source_path##*/}
  candidate_path=/var/lib/vonk-forge/incoming/$candidate_name
  candidate_stage=${candidate_path}.pending.$$
  install -o vonk-agent -g vonk-agent -m 0600 "$source_path" "$candidate_stage"
  sync -f "$candidate_stage"
  mv -f -- "$candidate_stage" "$candidate_path"
  sync -f /var/lib/vonk-forge/incoming
  printf '%s\n' "$candidate_path"
}

force_dpkg_status() {
  replacement=$1
  status_file=/var/lib/dpkg/status
  temporary=$test_root/dpkg-status
  awk -v replacement="$replacement" '
    BEGIN { RS=""; ORS="\n\n" }
    $0 ~ /(^|\n)Package: vonk-forge-agent(\n|$)/ {
      sub(/Status: [^\n]+/, "Status: " replacement)
    }
    { print }
  ' "$status_file" > "$temporary"
  chown root:root "$temporary"
  chmod 0644 "$temporary"
  sync -f "$temporary"
  mv -f -- "$temporary" "$status_file"
  sync -f /var/lib/dpkg
}

force_dpkg_version() {
  replacement=$1
  for status_file in /var/lib/dpkg/status /var/lib/dpkg/status-old; do
    temporary=$test_root/dpkg-${status_file##*/}
    awk -v replacement="$replacement" '
      BEGIN { RS=""; ORS="\n\n"; found=0 }
      $0 ~ /(^|\n)Package: vonk-forge-agent(\n|$)/ {
        if (sub(/Version: [^\n]+/, "Version: " replacement) != 1) exit 1
        found += 1
      }
      { print }
      END { if (found != 1) exit 1 }
    ' "$status_file" > "$temporary"
    chown root:root "$temporary"
    chmod 0644 "$temporary"
    sync -f "$temporary"
    mv -f -- "$temporary" "$status_file"
  done
  sync -f /var/lib/dpkg
}

atomic_replace() {
  local source=$1
  local destination=$2
  local mode=$3
  local temporary=${destination}.repair-fixture.$$
  install -o root -g root -m "$mode" "$source" "$temporary"
  sync -f "$temporary"
  mv -f -- "$temporary" "$destination"
  sync -f "${destination%/*}"
}

mutate_installed_file() {
  local destination=$1
  local mode=$2
  local temporary
  temporary=$test_root/mutated-$(basename "$destination")
  cp -- "$destination" "$temporary"
  printf x >> "$temporary"
  atomic_replace "$temporary" "$destination" "$mode"
}

run_wrong_binary_but_restore_installed() {
  local unit=$1
  local destination=$2
  local wrong_binary=$3
  local expected_installed=$4
  local gate_backup=
  local wrong_sha
  local restart_status
  local wrong_pid=
  local candidate_pid
  local wrong_pid_before
  local wrong_invocation_before
  local wrong_start_before
  local wrong_cgroup_before
  local wrong_exe_before
  local wrong_sha_before
  local wrong_owner_before
  local wrong_groups_before
  local gate_condition
  local source_gate_manager_path
  wrong_sha="$(sha256sum "$wrong_binary" | cut -d' ' -f1)"
  atomic_replace "$wrong_binary" "$destination" 0555
  if [[ "$unit" = "$agent_unit" ]]; then
    gate_backup=$test_root/running-agent-source-gate
    test -f "$source_gate"
    test ! -L "$source_gate"
    test "$(stat -c %u:%g:%a:%h "$source_gate")" = 0:0:644:1
    test "$(sha256sum "$source_gate" | cut -d' ' -f1)" = "$source_gate_sha"
    source_gate_manager_path="$(realpath -e -- "$source_gate")"
    test "$(systemctl --system show --property=DropInPaths --value \
      "$agent_unit")" = "$source_gate_manager_path"
    gate_condition="$(systemctl --system show \
      --property=ExecCondition --value "$agent_unit")"
    [[ "$gate_condition" = *"$source_runner"* ]]
    [[ "$gate_condition" = *"allow-agent-start"* ]]
    install -o root -g root -m 0644 "$source_gate" "$gate_backup"
    test "$(sha256sum "$gate_backup" | cut -d' ' -f1)" = "$source_gate_sha"
    rm -f -- "$source_gate"
    systemctl --system daemon-reload
    test -z "$(systemctl --system show --property=DropInPaths --value \
      "$agent_unit")"
    test -z "$(systemctl --system show --property=ExecCondition --value \
      "$agent_unit")"
  fi
  set +e
  systemctl --system restart "$unit"
  restart_status=$?
  set -e
  wrong_pid=
  if (( restart_status == 0 )); then
    for _ in {1..500}; do
      candidate_pid="$(systemctl --system show --property=MainPID --value \
        "$unit")"
      if [[ "$candidate_pid" =~ ^[0-9]+$ ]] && (( candidate_pid > 1 )) \
        && [[ -e "/proc/$candidate_pid/exe" ]] \
        && [[ "$(readlink "/proc/$candidate_pid/exe" 2>/dev/null)" \
          = "$destination" ]] \
        && [[ "$(sha256sum "/proc/$candidate_pid/exe" 2>/dev/null \
          | cut -d' ' -f1)" = "$wrong_sha" ]]; then
        wrong_pid=$candidate_pid
        break
      fi
      sleep 0.01
    done
  fi
  if [[ "$unit" = "$agent_unit" && -n "$wrong_pid" ]]; then
    wrong_pid_before="$(systemctl --system show --property=MainPID --value \
      "$unit")"
    wrong_invocation_before="$(systemctl --system show \
      --property=InvocationID --value "$unit")"
    wrong_start_before="$(awk '{print $22}' \
      "/proc/$wrong_pid_before/stat" 2>/dev/null)"
    wrong_cgroup_before="$(cat "/proc/$wrong_pid_before/cgroup" 2>/dev/null)"
    wrong_exe_before="$(readlink "/proc/$wrong_pid_before/exe" 2>/dev/null)"
    wrong_sha_before="$(sha256sum "/proc/$wrong_pid_before/exe" \
      2>/dev/null | cut -d' ' -f1)"
    wrong_owner_before="$(stat -c %u:%g "/proc/$wrong_pid_before" 2>/dev/null)"
    wrong_groups_before="$(awk '/^Groups:/ {
      $1=""; sub(/^[[:space:]]+/, ""); sub(/[[:space:]]+$/, ""); print
    }' "/proc/$wrong_pid_before/status" 2>/dev/null)"
  fi
  if [[ -n "$gate_backup" ]]; then
    atomic_replace "$gate_backup" "$source_gate" 0644
    systemctl --system daemon-reload
    cmp -s "$gate_backup" "$source_gate"
    test "$(stat -c %u:%g:%a:%h "$source_gate")" = 0:0:644:1
    test "$(sha256sum "$source_gate" | cut -d' ' -f1)" = "$source_gate_sha"
    test "$(realpath -e -- "$source_gate")" = "$source_gate_manager_path"
    test "$(systemctl --system show --property=DropInPaths --value \
      "$agent_unit")" = "$source_gate_manager_path"
    gate_condition="$(systemctl --system show \
      --property=ExecCondition --value "$agent_unit")"
    [[ "$gate_condition" = *"$source_runner"* ]]
    [[ "$gate_condition" = *"allow-agent-start"* ]]
  fi
  assert_fixture_equal "wrong-process restart status" 0 "$restart_status"
  if ! [[ "$wrong_pid" =~ ^[0-9]+$ ]] || (( wrong_pid <= 1 )); then
    printf 'wrong-process fixture mismatch: main pid expected=>1 observed=%s\n' \
      "$wrong_pid" >&2
    return 1
  fi
  assert_fixture_equal "wrong-process executable digest" "$wrong_sha" \
    "$(sha256sum "/proc/$wrong_pid/exe" | cut -d' ' -f1)"
  if [[ "$unit" = "$agent_unit" ]]; then
    assert_fixture_equal "wrong-process stable pid" "$wrong_pid_before" "$wrong_pid"
    assert_fixture_equal "wrong-process stable start time" "$wrong_start_before" \
      "$(awk '{print $22}' "/proc/$wrong_pid/stat")"
    assert_fixture_equal "wrong-process stable invocation" "$wrong_invocation_before" \
      "$(systemctl --system show --property=InvocationID --value "$unit")"
    if ! [[ "$wrong_invocation_before" =~ ^[0-9a-f]{32}$ ]]; then
      printf 'wrong-process fixture mismatch: invocation id observed=%s\n' \
        "$wrong_invocation_before" >&2
      return 1
    fi
    assert_fixture_equal "wrong-process cgroup identity" \
      "0::/system.slice/$agent_unit" "$wrong_cgroup_before"
    assert_fixture_equal "wrong-process stable cgroup" "$wrong_cgroup_before" \
      "$(cat "/proc/$wrong_pid/cgroup")"
    assert_fixture_equal "wrong-process uid and gid" "$agent_uid:$agent_gid" \
      "$wrong_owner_before"
    assert_fixture_equal "wrong-process supplementary groups" "$agent_groups" \
      "$wrong_groups_before"
    assert_fixture_equal "wrong-process executable path" "$destination" \
      "$wrong_exe_before"
    assert_fixture_equal "wrong-process captured digest" "$wrong_sha" \
      "$wrong_sha_before"
    assert_fixture_equal "wrong-process active state" active \
      "$(systemctl --system show --property=ActiveState --value "$unit")"
    assert_fixture_equal "wrong-process substate" running \
      "$(systemctl --system show --property=SubState --value "$unit")"
  fi
  atomic_replace "$expected_installed" "$destination" 0555
  [[ -f "$destination" && ! -L "$destination" ]]
  assert_fixture_equal "restored installed file custody" 0:0:555:1 \
    "$(stat -c %u:%g:%a:%h "$destination")"
  assert_fixture_equal "restored installed file digest" \
    "$(sha256sum "$expected_installed" | cut -d' ' -f1)" \
    "$(sha256sum "$destination" | cut -d' ' -f1)"
  assert_fixture_equal "running process retains wrong digest" "$wrong_sha" \
    "$(sha256sum "/proc/$wrong_pid/exe" | cut -d' ' -f1)"
  if [[ "$unit" = "$agent_unit" ]]; then
    assert_fixture_equal "running process deleted executable path" \
      "$destination (deleted)" "$(readlink "/proc/$wrong_pid/exe")"
  fi
}

move_main_process_to_wrong_cgroup() {
  unit=$1
  wrong_cgroup=/sys/fs/cgroup/vonk-forge-repair-fixture-$$
  mkdir "$wrong_cgroup"
  wrong_pid="$(systemctl --system show --property=MainPID --value "$unit")"
  test "$wrong_pid" -gt 1
  wrong_cgroup_pid=$wrong_pid
  printf '%s\n' "$wrong_pid" > "$wrong_cgroup/cgroup.procs"
  grep -Fxq "0::/${wrong_cgroup#/sys/fs/cgroup/}" "/proc/$wrong_pid/cgroup"
}

# Re-prove the labeled dev335 baseline after package construction and before
# deliberately applying any adversarial fixture mutations.
assert_old_fixture

case "$fault" in
  none) ;;
  wrong-node) printf 'node_id = "spk_00000000000000000000000000000000"\n' \
    > /etc/vonk-forge-agent/agent.toml ;;
  config-mode) chmod 0600 /etc/vonk-forge-agent/agent.toml ;;
  config-symlink)
    mv -- /etc/vonk-forge-agent/agent.toml "$test_root/agent.toml"
    ln -s -- "$test_root/agent.toml" /etc/vonk-forge-agent/agent.toml
    ;;
  direct-dpkg) ;;
  dpkg-iU) SYSTEMD_OFFLINE=1 dpkg --unpack --force-confold "$old_package" >/dev/null ;;
  dpkg-iF) force_dpkg_status 'install ok half-configured' ;;
  dpkg-iHR) force_dpkg_status 'install reinstreq half-installed' ;;
  absent) force_dpkg_status 'deinstall ok config-files' ;;
  newer) force_dpkg_status 'install ok installed'; sed -i \
    "s/^Version: $installed_version$/Version: $ordinary_version/" /var/lib/dpkg/status ;;
  installed-agent) mutate_installed_file /usr/lib/vonk-forge/vonk-agent 0555 ;;
  installed-helper) mutate_installed_file /usr/lib/vonk-forge/vonk-agent-helper 0555 ;;
  installed-agent-unit) mutate_installed_file \
    /lib/systemd/system/vonk-forge-agent.service 0644 ;;
  installed-helper-unit) mutate_installed_file \
    /lib/systemd/system/vonk-forge-package-helper.service 0644 ;;
  installed-socket-unit) mutate_installed_file \
    /lib/systemd/system/vonk-forge-package-helper.socket 0644 ;;
  running-agent)
    run_wrong_binary_but_restore_installed "$agent_unit" \
      /usr/lib/vonk-forge/vonk-agent "$test_root/target-bin/vonk-agent" \
      "$test_root/old-bin/vonk-agent"
    ;;
  running-helper)
    run_wrong_binary_but_restore_installed "$helper_unit" \
      /usr/lib/vonk-forge/vonk-agent-helper \
      "$test_root/target-bin/vonk-agent-helper" \
      "$test_root/old-bin/vonk-agent-helper"
    ;;
  cgroup-agent) move_main_process_to_wrong_cgroup "$agent_unit" ;;
  cgroup-helper) move_main_process_to_wrong_cgroup "$helper_unit" ;;
  source-intent) printf x >> "$source_intent" ;;
  source-cache) printf x >> "$source_state/$source_package_sha.deb" ;;
  source-runner) printf x >> "$source_runner" ;;
  source-unit) printf x >> "$source_unit" ;;
  source-gate) printf x >> "$source_gate" ;;
  source-dropin) printf x >> "$source_dropin" ;;
  source-blocker) printf x >> "$source_blocker" ;;
  source-pending) printf x >> "$source_pending" ;;
  source-lock-busy)
    ( flock -x 8; touch "$test_root/lock-held"; sleep 300 ) 8>> "$source_lock" &
    lock_holder=$!
    for _ in {1..200}; do [[ -e "$test_root/lock-held" ]] && break; sleep 0.01; done
    test -e "$test_root/lock-held"
    ;;
esac
case "$fault" in
  dpkg-iU|dpkg-iF|dpkg-iHR|absent|newer)
    synthetic_dpkg_fault_applied=true
    test -z "$(find /var/lib/dpkg/updates -mindepth 1 -maxdepth 1 \
      -print -quit)"
    cp -- /var/lib/dpkg/status "$test_root/before-dpkg-status"
    ;;
esac

custody=/run/vonk-forge-package-candidates/0123456789abcdef0123456789abcdef
install -d -o root -g root -m 0700 "$custody"
install -o root -g root -m 0600 "$repair_package" "$custody/$repair_sha.deb"
dispatch_candidate=$custody/$repair_sha.deb
if [[ "$fault" != direct-dpkg ]]; then
  dispatch_candidate="$(stage_helper_candidate "$dispatch_candidate")"
fi
fixture_agent_pid="$(systemctl --system show --property=MainPID --value "$agent_unit")"
fixture_helper_pid="$(systemctl --system show --property=MainPID --value "$helper_unit")"
snapshot_state "$test_root/before"
if [[ "$crash_phase" = pre-runner-rename ]]; then
  snapshot_source_authority_state "$test_root/before-source-authority" 'ii '
fi

crash_watcher=
if [[ "$crash_phase" = pre-runner-rename \
  || "$crash_phase" = post-runner-rename ]]; then
  (
    if [[ "$crash_phase" = pre-runner-rename ]]; then
      expected_handoff_runner=$source_runner_sha
    else
      expected_handoff_runner=$repair_runner_sha
    fi
    for _ in {1..60000}; do
      for hidden_phase in "$source_state"/.repair-build.*/phase; do
        hidden_tree=${hidden_phase%/phase}
        if grep -Fxq 'phase=armed' "$hidden_phase" 2>/dev/null \
          && [[ -f "$hidden_tree/authority" \
            && -f "$hidden_tree/intent" \
            && -f "$hidden_tree/agent-blocked" \
            && -f "$hidden_tree/candidate.deb" ]]; then
          systemctl --system freeze "$helper_unit"
          if [[ "$(sha256sum "$source_runner" | cut -d' ' -f1)" \
            != "$expected_handoff_runner" ]]; then
            systemctl --system thaw "$helper_unit"
            continue
          fi
          # The freeze brackets the atomic main-runner rename without adding a
          # production hook. Durably sync and prove there is exactly one fully
          # prepared tree before modelling power loss.
          sync -f "$source_state"
          test "$(find "$source_state" -mindepth 1 -maxdepth 1 -type d \
            -name '.repair-build.*' | wc -l)" -eq 1
          snapshot_prepared_objects "$test_root/prepared-before"
          if [[ "$crash_phase" = pre-runner-rename ]]; then
            snapshot_source_authority_state \
              "$test_root/pre-runner-authority" iHR
            if ! cmp -s "$test_root/before-source-authority" \
              "$test_root/pre-runner-authority"; then
              diff -u "$test_root/before-source-authority" \
                "$test_root/pre-runner-authority" >&2 || true
              exit 1
            fi
          fi
          systemctl --system kill --kill-whom=all --signal=SIGKILL "$helper_unit"
          touch "$test_root/crash-observed"
          exit 0
        fi
      done
      sleep 0.001
    done
    exit 1
  ) &
  crash_watcher=$!
elif [[ "$crash_phase" != none ]]; then
  (
    observed_phase=${crash_phase%-boot}
    for _ in {1..12000}; do
      if grep -Fxq "phase=$observed_phase" "$repair_phase" 2>/dev/null \
        && [[ "$(systemctl --system show --property=ActiveState --value \
          "$recovery_unit")" =~ ^(activating|active)$ ]]; then
        systemctl --system kill --kill-whom=all --signal=SIGKILL "$recovery_unit"
        touch "$test_root/crash-observed"
        exit 0
      fi
      sleep 0.005
    done
    exit 1
  ) &
  crash_watcher=$!
fi

rm -f -- "$result" "$started"
if [[ "$fault" = direct-dpkg ]]; then
  set +e
  /usr/bin/dpkg --install --force-confold "$custody/$repair_sha.deb"
  direct_status=$?
  set -e
  printf '%s d\n' "$direct_status" > "$result"
else
  submit_helper_install "$dispatch_candidate"
fi

if [[ "$fault" != none ]]; then
  for _ in {1..3000}; do [[ -s "$result" ]] && break; sleep 0.01; done
  test -s "$result"
  test "$(awk '{print $1}' "$result")" -ne 0
  snapshot_state "$test_root/after"
  case "$fault" in
    dpkg-iU) assert_exact_dpkg_transition \
      "iU |arm64|$installed_version" "iU |arm64|$installed_version" ;;
    dpkg-iF) assert_exact_dpkg_transition \
      "iF |arm64|$installed_version" "ii |arm64|$installed_version" ;;
    dpkg-iHR) assert_exact_dpkg_transition \
      "iHR|arm64|$installed_version" "iH |arm64|$installed_version" ;;
    absent) assert_exact_dpkg_transition \
      "rc |arm64|$installed_version" "ic |arm64|$installed_version" ;;
    newer) assert_exact_dpkg_transition \
      "ii |arm64|$ordinary_version" "ii |arm64|$ordinary_version" ;;
    *) cmp -s "$test_root/before" "$test_root/after" ;;
  esac
  test "$(systemctl --system show --property=MainPID --value "$agent_unit")" \
    = "$fixture_agent_pid"
  test "$(systemctl --system show --property=MainPID --value "$helper_unit")" \
    = "$fixture_helper_pid"
  test ! -e "$repair_state"
  assert_repair_probe_not_persisted
  printf 'node repair adversarial no-mutation %s: PASS\n' "$fault"
  exit 0
fi

if [[ -n "$crash_watcher" ]]; then
  wait "$crash_watcher"
  test -e "$test_root/crash-observed"
  if [[ " ${repair_boot_crashpoints[*]} " = *" $crash_phase "* ]]; then
    systemctl --system thaw "$helper_unit" >/dev/null 2>&1 || true
    systemctl --system stop "$recovery_unit" "$agent_unit" "$helper_unit" \
      "$socket_unit" >/dev/null 2>&1 || true
    if [[ "$crash_phase" = helper-proven-boot \
      || "$crash_phase" = agent-proven-boot ]]; then
      stale_receipt=$test_root/stale-helper-receipt
      sed 's/^boot_id=.*/boot_id=00000000-0000-0000-0000-000000000001/' \
        "$helper_receipt" > "$stale_receipt"
      atomic_replace "$stale_receipt" "$helper_receipt" 0600
    fi
    systemctl --system reset-failed "$recovery_unit" "$agent_unit" \
      "$helper_unit" "$socket_unit" >/dev/null 2>&1 || true
    systemctl --system daemon-reload
    systemctl --system start "$socket_unit"
  else
    systemctl --system reset-failed "$recovery_unit" >/dev/null 2>&1 || true
    systemctl --system start "$recovery_unit" >/dev/null 2>&1 || true
  fi
fi

if [[ "$crash_phase" = pre-runner-rename ]]; then
  for _ in {1..2400}; do
    if [[ ! -e "$source_intent" \
      && "$(dpkg-query -W -f='${Version}' vonk-forge-agent 2>/dev/null)" \
        = "$source_version" ]]; then
      break
    fi
    sleep 0.1
  done
  test "$(dpkg-query -W -f='${db:Status-Abbrev}' vonk-forge-agent)" = 'ii '
  test "$(dpkg-query -W -f='${Version}' vonk-forge-agent)" = "$source_version"
  test ! -e "$repair_state"
  test ! -e "$repair_receipt"
  test ! -e "$helper_receipt"
  assert_repair_probe_not_persisted
  test "$(sha256sum "$source_runner" | cut -d' ' -f1)" = "$source_runner_sha"
  source_agent_pid="$(systemctl --system show --property=MainPID --value "$agent_unit")"
  source_helper_pid="$(systemctl --system show --property=MainPID --value "$helper_unit")"
  test "$(sha256sum "/proc/$source_agent_pid/exe" | cut -d' ' -f1)" \
    = "$source_agent_sha"
  test "$(sha256sum "/proc/$source_helper_pid/exe" | cut -d' ' -f1)" \
    = "$source_helper_sha"
  snapshot_prepared_objects "$test_root/prepared-after"
  cmp -s "$test_root/prepared-before" "$test_root/prepared-after"
  printf '%s\n' \
    'pre-runner-rename boot preserves ignored prepared objects and baseline a122 recovery: PASS'
  exit 0
fi

for _ in {1..2400}; do
  if [[ "$crash_phase" = post-runner-rename && ! -e "$source_intent" \
    && "$(dpkg-query -W -f='${Version}' vonk-forge-agent 2>/dev/null)" \
      != "$repair_version" ]]; then
    printf '%s\n' \
      'post-runner-rename boot converged outside the node-bound repair version' >&2
    exit 1
  fi
  if [[ -f "$repair_receipt" && ! -e "$source_intent" \
    && "$(dpkg-query -W -f='${db:Status-Abbrev}' vonk-forge-agent 2>/dev/null)" \
      = 'ii ' ]]; then
    break
  fi
  sleep 0.1
done
test -f "$repair_receipt"
test -f "$helper_receipt"
test "$(stat -c %u:%g:%a:%h "$repair_receipt")" = 0:0:600:1
test "$(stat -c %u:%g:%a:%h "$helper_receipt")" = 0:0:600:1
test "$(wc -l < "$repair_receipt")" -eq 16
test "$(wc -l < "$helper_receipt")" -eq 10
sed -n '1p' "$helper_receipt" | grep -Fxq 'schema_version=1'
sed -n '2p' "$helper_receipt" | grep -Fxq "authority_sha256=$authority_sha"
helper_nonce="$(sed -n '3s/^repair_nonce=//p' "$helper_receipt")"
[[ "$helper_nonce" =~ ^[0-9a-f]{64}$ ]]
sed -n '4p' "$helper_receipt" | grep -Fxq "version=$repair_version"
sed -n '5p' "$helper_receipt" | grep -Fxq "helper_sha256=$source_helper_sha"
sed -n '6p' "$helper_receipt" | grep -Fxq "agent_sha256=$source_agent_sha"
sed -n '7p' "$helper_receipt" | grep -Fxq \
  "source_intent_sha256=$source_intent_sha"
sed -n '8p' "$helper_receipt" | grep -Fxq "boot_id=$boot_id"
helper_receipt_pid="$(sed -n '9s/^helper_main_pid=//p' "$helper_receipt")"
helper_receipt_start="$(sed -n '10s/^helper_start_time=//p' "$helper_receipt")"
[[ "$helper_receipt_pid" =~ ^[1-9][0-9]*$ ]]
[[ "$helper_receipt_start" =~ ^[1-9][0-9]*$ ]]

sed -n '1p' "$repair_receipt" | grep -Fxq 'schema_version=1'
sed -n '2p' "$repair_receipt" | grep -Fxq "authority_sha256=$authority_sha"
sed -n '3p' "$repair_receipt" | grep -Fxq "node_id=$node_id"
sed -n '4p' "$repair_receipt" | grep -Fxq "version=$repair_version"
sed -n '5p' "$repair_receipt" | grep -Fxq \
  "source_intent_sha256=$source_intent_sha"
sed -n '6p' "$repair_receipt" | grep -Fxq "candidate_sha256=$repair_sha"
sed -n '7p' "$repair_receipt" | grep -Fxq \
  "repair_runner_sha256=$repair_runner_sha"
sed -n '8p' "$repair_receipt" | grep -Fxq "helper_sha256=$source_helper_sha"
final_helper_pid="$(sed -n '9s/^helper_main_pid=//p' "$repair_receipt")"
final_helper_start="$(sed -n '10s/^helper_start_time=//p' "$repair_receipt")"
sed -n '11p' "$repair_receipt" | grep -Fxq "agent_sha256=$source_agent_sha"
final_agent_pid="$(sed -n '12s/^agent_main_pid=//p' "$repair_receipt")"
final_agent_start="$(sed -n '13s/^agent_start_time=//p' "$repair_receipt")"
sed -n '14p' "$repair_receipt" | grep -Fxq "boot_id=$boot_id"
sed -n '15p' "$repair_receipt" \
  | grep -Eq '^activated_at=[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'
final_nonce="$(sed -n '16s/^repair_nonce=//p' "$repair_receipt")"
test "$final_nonce" = "$helper_nonce"
test "$final_helper_pid" = "$helper_receipt_pid"
test "$final_helper_start" = "$helper_receipt_start"
[[ "$final_agent_pid" =~ ^[1-9][0-9]*$ ]]
[[ "$final_agent_start" =~ ^[1-9][0-9]*$ ]]
test ! -e "$source_intent"
test ! -e "$source_state/$source_package_sha.deb"
test ! -e "$source_blocker"
test ! -e "$source_pending"
test ! -e "$source_dropin"
test ! -e "$repair_state"
test "$(find "$source_state" -mindepth 1 -maxdepth 1 -type d \
  -name '.repair-build.*' | wc -l)" -eq 0
test "$(dpkg-query -W -f='${Version}' vonk-forge-agent)" = "$repair_version"
test "$(stat -c %u:%g:%a:%h "$standard_runner")" = 0:0:555:1
test "$(sha256sum "$standard_runner" | cut -d' ' -f1)" \
  = "$(sha256sum "$repair_standard_payload" | cut -d' ' -f1)"
assert_repair_probe_not_persisted
target_agent_pid="$(systemctl --system show --property=MainPID --value "$agent_unit")"
target_helper_pid="$(systemctl --system show --property=MainPID --value "$helper_unit")"
test "$target_agent_pid" = "$final_agent_pid"
test "$target_helper_pid" = "$final_helper_pid"
test "$(awk '{print $22}' "/proc/$target_agent_pid/stat")" = "$final_agent_start"
test "$(awk '{print $22}' "/proc/$target_helper_pid/stat")" \
  = "$final_helper_start"
test "$(stat -c %u "/proc/$target_agent_pid")" = "$(id -u vonk-agent)"
test "$(stat -c %u "/proc/$target_helper_pid")" = 0
test "$(awk '/^Uid:/ { print $2 ":" $3 ":" $4 ":" $5 }' "/proc/$target_agent_pid/status")" = "$agent_uid:$agent_uid:$agent_uid:$agent_uid"
test "$(awk '/^Gid:/ { print $2 ":" $3 ":" $4 ":" $5 }' "/proc/$target_agent_pid/status")" = "$agent_gid:$agent_gid:$agent_gid:$agent_gid"
test "$(awk '/^Uid:/ { print $2 ":" $3 ":" $4 ":" $5 }' "/proc/$target_helper_pid/status")" = 0:0:0:0
test "$(awk '/^Gid:/ { print $2 ":" $3 ":" $4 ":" $5 }' "/proc/$target_helper_pid/status")" = 0:0:0:0
test "$(sha256sum "/proc/$target_agent_pid/exe" | cut -d' ' -f1)" = "$source_agent_sha"
test "$(sha256sum "/proc/$target_helper_pid/exe" | cut -d' ' -f1)" = "$source_helper_sha"
test "$(systemctl --system show --property=ControlGroup --value "$agent_unit")" \
  = "/system.slice/$agent_unit"
test "$(systemctl --system show --property=ControlGroup --value "$helper_unit")" \
  = "/system.slice/$helper_unit"
grep -Fxq "0::/system.slice/$agent_unit" "/proc/$target_agent_pid/cgroup"
grep -Fxq "0::/system.slice/$helper_unit" "/proc/$target_helper_pid/cgroup"
test "$target_agent_pid" != "$old_agent_pid"
test "$target_helper_pid" != "$old_helper_pid"
self_test="$(/usr/lib/vonk-forge/vonk-agent --config \
  /etc/vonk-forge-agent/agent.toml self-test)"
grep -F '"semantic_version":"0.1.0"' <<< "$self_test" >/dev/null
grep -F '"build_digest":"'"$build_digest_target"'"' <<< "$self_test" >/dev/null
grep -F '"binary_digest":"'"$source_agent_sha"'"' <<< "$self_test" >/dev/null
grep -F '"architecture":"linux-arm64"' <<< "$self_test" >/dev/null
grep -F '"self_test_passed":true' <<< "$self_test" >/dev/null
observation_receipt_private=/var/lib/vonk-forge/helper/observation-receipt.pk8
observation_receipt_public=/etc/vonk-forge-agent/observation-receipt.pub
observation_receipt_der=$test_root/observation-receipt-public.der
observation_receipt_derived=$test_root/observation-receipt-derived.pub
test "$(stat -c '%U:%G:%a:%h' "$observation_receipt_private")" \
  = root:root:600:1
test "$(stat -c '%U:%G:%a:%h' "$observation_receipt_public")" \
  = root:vonk-agent:640:1
test "$(wc -c < "$observation_receipt_public")" -eq 32
openssl pkey -inform DER -in "$observation_receipt_private" \
  -check -noout >/dev/null 2>&1
openssl pkey -inform DER -in "$observation_receipt_private" \
  -pubout -outform DER -out "$observation_receipt_der" >/dev/null 2>&1
test "$(wc -c < "$observation_receipt_der")" -eq 44
tail -c 32 "$observation_receipt_der" > "$observation_receipt_derived"
cmp -s "$observation_receipt_derived" "$observation_receipt_public"
observation_receipt_private_digest=$(sha256sum \
  "$observation_receipt_private" | cut -d' ' -f1)

# Prove that the repaired helper can carry one subsequent ordinary package
# through the same root-custody + dpkg parent-chain mechanism.
build_package "$ordinary_version" next "$build_digest_next" "$test_root/next-dist"
ordinary_package=$test_root/next-dist/vonk-forge-agent_${ordinary_version}_arm64.deb
"$repo_root/scripts/verify-agent-deb" --json "$ordinary_package" >/dev/null
ordinary_sha="$(sha256sum "$ordinary_package" | cut -d' ' -f1)"
ordinary_custody=/run/vonk-forge-package-candidates/fedcba9876543210fedcba9876543210
install -d -o root -g root -m 0700 "$ordinary_custody"
install -o root -g root -m 0600 "$ordinary_package" \
  "$ordinary_custody/$ordinary_sha.deb"
ordinary_dispatch="$(stage_helper_candidate \
  "$ordinary_custody/$ordinary_sha.deb")"
rm -f -- "$result" "$started"
submit_helper_install "$ordinary_dispatch"
for _ in {1..2400}; do
  if [[ "$(dpkg-query -W -f='${Version}' vonk-forge-agent 2>/dev/null)" \
      = "$ordinary_version" \
    && "$(dpkg-query -W -f='${db:Status-Abbrev}' vonk-forge-agent 2>/dev/null)" \
      = 'ii ' && -s "$started" \
    && -f /var/lib/vonk-forge/helper-upgrade.receipt \
    && ! -e "$source_intent" && ! -e "$source_pending" ]]; then
    break
  fi
  sleep 0.1
done
test "$(dpkg-query -W -f='${Version}' vonk-forge-agent)" = "$ordinary_version"
test "$(sed -n '1p' "$started")" = t
grep -Fxq 'schema_version=2' /var/lib/vonk-forge/helper-upgrade.receipt
grep -Fxq "version=$ordinary_version" /var/lib/vonk-forge/helper-upgrade.receipt
grep -Fxq "package_sha256=$ordinary_sha" \
  /var/lib/vonk-forge/helper-upgrade.receipt
test ! -e "$source_intent"
test ! -e "$source_pending"
test ! -e "$source_state/$ordinary_sha.deb"
assert_repair_probe_not_persisted
ordinary_agent_sha="$(sha256sum "$test_root/next-bin/vonk-agent" | cut -d' ' -f1)"
for _ in {1..1200}; do
  ordinary_agent_pid="$(systemctl --system show --property=MainPID --value "$agent_unit")"
  if [[ "$ordinary_agent_pid" -gt 1 \
    && "$(sha256sum "/proc/$ordinary_agent_pid/exe" 2>/dev/null | cut -d' ' -f1)" \
      = "$ordinary_agent_sha" ]]; then
    break
  fi
  sleep 0.1
done
test "$(sha256sum "/proc/$ordinary_agent_pid/exe" | cut -d' ' -f1)" \
  = "$ordinary_agent_sha"
ordinary_self_test="$(/usr/lib/vonk-forge/vonk-agent --config \
  /etc/vonk-forge-agent/agent.toml self-test)"
grep -F '"build_digest":"'"$build_digest_next"'"' \
  <<< "$ordinary_self_test" >/dev/null
grep -F '"binary_digest":"'"$ordinary_agent_sha"'"' \
  <<< "$ordinary_self_test" >/dev/null
grep -F '"self_test_passed":true' <<< "$ordinary_self_test" >/dev/null
test "$(sha256sum "$observation_receipt_private" | cut -d' ' -f1)" \
  = "$observation_receipt_private_digest"
cmp -s "$observation_receipt_derived" "$observation_receipt_public"

printf 'dev335 -> a122 node repair phase=%s and ordinary helper upgrade: PASS\n' \
  "$crash_phase"

#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
  printf '%s\n' 'agent upgrade bridge sandbox test must run as root' >&2
  exit 77
fi
if [[ ! -d /run/systemd/system ]]; then
  printf '%s\n' 'agent upgrade bridge sandbox test requires systemd' >&2
  exit 77
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
unit=vonk-forge-package-helper.service
bridge_job=vonk-forge-package-helper-upgrade-bridge.service
unit_path=/run/systemd/system/$unit
dropin_dir=/lib/systemd/system/$unit.d
dropin=$dropin_dir/20-package-upgrade-bridge.conf
runtime=/run/vonk-forge-package-helper
test_root="$(mktemp -d /var/lib/vonk-forge-bridge-test.XXXXXX)"

cleanup() {
  systemctl --system stop "$unit" "$bridge_job" >/dev/null 2>&1 || true
  systemctl --system reset-failed "$unit" "$bridge_job" >/dev/null 2>&1 || true
  rm -f -- "$unit_path" "$dropin" \
    "$dropin_dir/20-package-upgrade-bridge.conf.retired"
  rm -rf -- "$runtime/upgrade-bridge"
  rm -f -- /var/lib/vonk-forge/helper-upgrade.pending \
    /var/lib/vonk-forge/helper-upgrade.receipt
  rm -f -- "$runtime/sandbox-first-pid" "$runtime/sandbox-second-pid" \
    "$runtime/sandbox-preinst.log"
  rmdir --ignore-fail-on-non-empty /usr/share/doc/vonk-forge-agent \
    /usr/share/keyrings 2>/dev/null || true
  rmdir --ignore-fail-on-non-empty "$dropin_dir" 2>/dev/null || true
  systemctl --system daemon-reload >/dev/null 2>&1 || true
  rm -rf -- "$test_root"
}
trap cleanup EXIT HUP INT TERM

if systemctl --system cat "$unit" >/dev/null 2>&1 \
  || [[ -e "$unit_path" || -L "$unit_path" \
    || -e "$dropin" || -L "$dropin" ]]; then
  printf '%s\n' 'agent upgrade bridge sandbox fixture would collide with a host unit' >&2
  exit 1
fi

sed "s/@VERSION@/0.1.0/g" "$repo_root/packaging/debian/preinst" \
  > "$test_root/preinst"
chmod 0755 "$test_root/preinst"
install -d -o root -g root -m 0755 \
  /usr/share/keyrings /usr/share/doc/vonk-forge-agent /var/lib/vonk-forge

cat > "$test_root/helper-wrapper" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
runtime=/run/vonk-forge-package-helper
if "${VONK_BRIDGE_PREINST:?}" upgrade 0.0.9 \
  2>> "$runtime/sandbox-preinst.log"; then
  printf '%s\n' "$BASHPID" > "$runtime/sandbox-second-pid"
  exec /bin/sleep 300
else
  status=$?
fi
[[ "$status" -eq 1 ]]
grep -Fq 'package-helper upgrade bridge staged' \
  "$runtime/sandbox-preinst.log"
printf '%s\n' "$BASHPID" > "$runtime/sandbox-first-pid"
WRAPPER
chmod 0755 "$test_root/helper-wrapper"

cat > "$unit_path" <<UNIT
[Unit]
Description=Vonk Forge dev335 upgrade bridge sandbox fixture

[Service]
Type=simple
ExecStart=$test_root/helper-wrapper
Environment=VONK_BRIDGE_PREINST=$test_root/preinst
User=root
Group=root
ProtectSystem=strict
ProtectHome=yes
NoNewPrivileges=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX
RuntimeDirectory=vonk-forge-package-helper
RuntimeDirectoryMode=0711
RuntimeDirectoryPreserve=yes
ReadWritePaths=/lib/systemd/system /var/lib/vonk-forge
TimeoutStopSec=15s
UNIT
chmod 0644 "$unit_path"
systemctl --system daemon-reload
systemctl --system start "$unit"

for _ in {1..300}; do
  [[ -f "$runtime/sandbox-second-pid" ]] && break
  sleep 0.1
done
test -f "$runtime/sandbox-first-pid"
test -f "$runtime/sandbox-second-pid"
test -f /var/lib/vonk-forge/helper-upgrade.pending
test ! -L /var/lib/vonk-forge/helper-upgrade.pending
test "$(stat -c %U:%G:%a /var/lib/vonk-forge/helper-upgrade.pending)" = \
  root:root:600
test "$(wc -l < /var/lib/vonk-forge/helper-upgrade.pending)" -eq 2
grep -Fxq 'version=0.1.0' /var/lib/vonk-forge/helper-upgrade.pending
grep -Fxq 'state=pre-unpack' /var/lib/vonk-forge/helper-upgrade.pending
test -f "$dropin"
test ! -L "$dropin"
test "$(stat -c %U:%G:%a "$dropin")" = root:root:644
test "$(wc -l < "$dropin")" -eq 2
grep -Fxq '[Service]' "$dropin"
grep -Fxq \
  'ReadWritePaths=/usr/share/keyrings /usr/share/doc/vonk-forge-agent' \
  "$dropin"

first_pid="$(cat "$runtime/sandbox-first-pid")"
second_pid="$(cat "$runtime/sandbox-second-pid")"
marker_pid="$(cat "$runtime/upgrade-bridge/previous-main-pid")"
test "$first_pid" != "$second_pid"
test "$marker_pid" = "$first_pid"
test "$(systemctl --system show --property=MainPID --value "$unit")" = \
  "$second_pid"

printf '%s\n' 'dev335 ProtectSystem upgrade bridge sandbox: PASS'

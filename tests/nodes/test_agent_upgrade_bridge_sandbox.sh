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
permanent_unit=/lib/systemd/system/$unit
dropin_dir=/lib/systemd/system/$unit.d
dropin=$dropin_dir/20-package-upgrade-bridge.conf
runtime=/run/vonk-forge-package-helper
test_root="$(mktemp -d /var/lib/vonk-forge-bridge-test.XXXXXX)"

cleanup() {
  systemctl --system stop "$unit" "$bridge_job" >/dev/null 2>&1 || true
  systemctl --system reset-failed "$unit" "$bridge_job" >/dev/null 2>&1 || true
  rm -f -- "$unit_path" "$permanent_unit" "$dropin" \
    "$dropin_dir/20-package-upgrade-bridge.conf.retired"
  rm -rf -- "$runtime/upgrade-bridge"
  rm -f -- /var/lib/vonk-forge/helper-upgrade.pending \
    /var/lib/vonk-forge/helper-upgrade.receipt
  rm -f -- "$runtime/sandbox-first-pid" "$runtime/sandbox-second-pid" \
    "$runtime/sandbox-preinst.log" "$runtime/sandbox-action" \
    "$runtime/partial-first-pid" "$runtime/partial-second-pid" \
    "$runtime/partial-preinst.log" "$runtime/partial-action"
  rmdir --ignore-fail-on-non-empty /usr/share/doc/vonk-forge-agent \
    /usr/share/keyrings 2>/dev/null || true
  rmdir --ignore-fail-on-non-empty "$dropin_dir" 2>/dev/null || true
  systemctl --system daemon-reload >/dev/null 2>&1 || true
  rm -rf -- "$test_root"
}
trap cleanup EXIT HUP INT TERM

if systemctl --system cat "$unit" >/dev/null 2>&1 \
  || [[ -e "$unit_path" || -L "$unit_path" \
    || -e "$permanent_unit" || -L "$permanent_unit" \
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
scenario=${VONK_BRIDGE_SCENARIO:?}
case "$scenario" in
  healthy)
    prefix=sandbox
    action=upgrade-with-old-version
    arguments=(upgrade 0.0.9)
    ;;
  partial)
    prefix=partial
    action=install-without-old-version
    arguments=(install)
    ;;
  *) exit 2 ;;
esac
printf '%s\n' "$action" > "$runtime/$prefix-action"
if "${VONK_BRIDGE_PREINST:?}" "${arguments[@]}" \
  2>> "$runtime/$prefix-preinst.log"; then
  printf '%s\n' "$BASHPID" > "$runtime/$prefix-second-pid"
  exec /bin/sleep 300
else
  status=$?
fi
[[ "$status" -eq 1 ]]
grep -Fq 'package-helper upgrade bridge staged' \
  "$runtime/$prefix-preinst.log"
printf '%s\n' "$BASHPID" > "$runtime/$prefix-first-pid"
WRAPPER
chmod 0755 "$test_root/helper-wrapper"

cat > "$unit_path" <<UNIT
[Unit]
Description=Vonk Forge dev335 upgrade bridge sandbox fixture

[Service]
Type=simple
ExecStart=$test_root/helper-wrapper
Environment=VONK_BRIDGE_PREINST=$test_root/preinst
Environment=VONK_BRIDGE_SCENARIO=healthy
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

# Reproduce the physical reinstreq state: a failed earlier unpack has already
# replaced the on-disk base unit with the target unit, but the running helper
# still owns the old read-only mount namespace. Dpkg may invoke the repair as
# `preinst install` without an old-version argument. The on-disk unit must not
# be accepted as proof that either package directory is writable.
systemctl --system stop "$unit"
rm -f -- "$dropin" /var/lib/vonk-forge/helper-upgrade.pending
rm -rf -- "$runtime/upgrade-bridge"
rm -f -- "$runtime/sandbox-first-pid" "$runtime/sandbox-second-pid" \
  "$runtime/sandbox-preinst.log" "$runtime/sandbox-action"
cat > "$permanent_unit" <<'PERMANENT'
[Service]
ReadWritePaths=/usr/share/keyrings /usr/share/doc/vonk-forge-agent
PERMANENT
chmod 0644 "$permanent_unit"
sed -i \
  's/Environment=VONK_BRIDGE_SCENARIO=healthy/Environment=VONK_BRIDGE_SCENARIO=partial/' \
  "$unit_path"
systemctl --system daemon-reload
systemctl --system start "$unit"

for _ in {1..300}; do
  [[ -f "$runtime/partial-second-pid" ]] && break
  sleep 0.1
done
test -f "$runtime/partial-first-pid"
test -f "$runtime/partial-second-pid"
grep -Fxq 'install-without-old-version' "$runtime/partial-action"
grep -Fq 'package-helper upgrade bridge staged' \
  "$runtime/partial-preinst.log"
test -f "$permanent_unit"
grep -Fxq \
  'ReadWritePaths=/usr/share/keyrings /usr/share/doc/vonk-forge-agent' \
  "$permanent_unit"
test -f "$dropin"
partial_first_pid="$(cat "$runtime/partial-first-pid")"
partial_second_pid="$(cat "$runtime/partial-second-pid")"
partial_marker_pid="$(cat "$runtime/upgrade-bridge/previous-main-pid")"
test "$partial_first_pid" != "$partial_second_pid"
test "$partial_marker_pid" = "$partial_first_pid"
test "$(systemctl --system show --property=MainPID --value "$unit")" = \
  "$partial_second_pid"
effective_paths="$(systemctl --system show --property=ReadWritePaths \
  --value "$unit")"
grep -Fq '/usr/share/keyrings' <<< "$effective_paths"
grep -Fq '/usr/share/doc/vonk-forge-agent' <<< "$effective_paths"
if compgen -G '/usr/share/keyrings/.vonk-package-write.*' >/dev/null \
  || compgen -G '/usr/share/doc/vonk-forge-agent/.vonk-package-write.*' \
    >/dev/null; then
  printf '%s\n' 'package-directory writability probe leaked a file' >&2
  exit 1
fi

printf '%s\n' \
  'dev335 healthy and partially-unpacked ProtectSystem bridge sandbox: PASS'

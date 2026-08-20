#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
drop_in="$repo_root/nodes/etc/ssh/sshd_config.d/90-vonk-admin.conf"
installer="$repo_root/nodes/bin/install-ssh-hardening"

expected="$(printf '%s\n' \
  'PasswordAuthentication no' \
  'KbdInteractiveAuthentication no' \
  'PubkeyAuthentication yes' \
  'PermitRootLogin prohibit-password')"
test "$(cat "$drop_in")" = "$expected"
bash -n "$installer"

set +e
bash "$installer" --check > /tmp/vonk-hardening-usage.out 2>&1
usage_rc=$?
set -e
test "$usage_rc" -eq 64
grep -Fq -- '--admin-user USER' /tmp/vonk-hardening-usage.out
grep -Fq -- '--admin-key-fingerprint SHA256:' /tmp/vonk-hardening-usage.out
grep -Fq -- '--drop-in FILE' /tmp/vonk-hardening-usage.out
rm -f /tmp/vonk-hardening-usage.out

printf 'SSH hardening artifacts: PASS\n'

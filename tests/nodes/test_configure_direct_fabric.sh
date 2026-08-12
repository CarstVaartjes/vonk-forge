#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/nodes/bin/configure-direct-fabric"
fixture_dir="$(mktemp -d)"
trap 'rm -rf -- "$fixture_dir"' EXIT

cat > "$fixture_dir/node2.expected" <<'EXPECTED'
network:
  version: 2
  ethernets:
    enp1s0f1np1:
      addresses:
        - 192.168.100.11/24
      dhcp4: false
      mtu: 1500
      optional: true
    enP2p1s0f1np1:
      addresses:
        - 192.168.101.11/24
      dhcp4: false
      mtu: 1500
      optional: true
EXPECTED

"$script" --node node2 --emit-netplan > "$fixture_dir/node2.actual"
cmp "$fixture_dir/node2.expected" "$fixture_dir/node2.actual"

cat > "$fixture_dir/node1.expected" <<'EXPECTED'
network:
  version: 2
  ethernets:
    enp1s0f1np1:
      addresses:
        - 192.168.100.10/24
      dhcp4: false
      mtu: 1500
      optional: true
    enP2p1s0f1np1:
      addresses:
        - 192.168.101.10/24
      dhcp4: false
      mtu: 1500
      optional: true
EXPECTED

"$script" --node node1 --emit-netplan > "$fixture_dir/node1.actual"
cmp "$fixture_dir/node1.expected" "$fixture_dir/node1.actual"

if "$script" --node spark3 --emit-netplan > "$fixture_dir/invalid.out" 2>&1; then
  printf 'script accepted an unsupported node\n' >&2
  exit 1
fi
grep -Fq 'node1 or node2' "$fixture_dir/invalid.out"

if grep -Eq 'id_ed25519_shared|Host \*|ssh-copy-id|scp .*id_ed25519' "$script"; then
  printf 'script contains prohibited shared-key or broad SSH configuration behavior\n' >&2
  exit 1
fi

grep -Fq -- '--apply is retired; use NVIDIA Sync Cluster Assistant' "$script"
if grep -Eq '^apply\(\)' "$script"; then
  printf 'script still contains a manual fabric installation path\n' >&2
  exit 1
fi
grep -Fq 'netplan try' "$script"
grep -Fq 'ip route show default dev' "$script"
grep -Fq 'ForwardAgent=no' "$script"

printf 'direct fabric configuration script: PASS\n'

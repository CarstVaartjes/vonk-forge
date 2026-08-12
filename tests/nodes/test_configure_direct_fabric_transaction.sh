#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/nodes/bin/configure-direct-fabric"
fixture_dir="$(mktemp -d)"
trap 'rm -rf -- "$fixture_dir"' EXIT

netplan_dir="$fixture_dir/netplan"
sys_net="$fixture_dir/sys/class/net"
sys_ib="$fixture_dir/sys/class/infiniband"
state_file="$fixture_dir/runtime-state"
rollback_dir="$fixture_dir/rollback"
mkdir -p "$netplan_dir" "$sys_net" "$sys_ib" "$fixture_dir/bin"
printf 'baseline\n' > "$state_file"

for interface in enp1s0f1np1 enP2p1s0f1np1; do
  mkdir -p "$sys_net/$interface"
  printf 'up\n' > "$sys_net/$interface/operstate"
  printf '200000\n' > "$sys_net/$interface/speed"
  printf '1500\n' > "$sys_net/$interface/mtu"
done

make_gid() {
  local hca="$1" interface="$2" address="$3"
  mkdir -p "$sys_ib/$hca/ports/1/gids" \
    "$sys_ib/$hca/ports/1/gid_attrs/types" \
    "$sys_ib/$hca/ports/1/gid_attrs/ndevs"
  printf '%s\n' "$address" > "$sys_ib/$hca/ports/1/gids/3"
  printf 'RoCE v2\n' > "$sys_ib/$hca/ports/1/gid_attrs/types/3"
  printf '%s\n' "$interface" > "$sys_ib/$hca/ports/1/gid_attrs/ndevs/3"
}

make_gid rocep1s0f1 enp1s0f1np1 ::ffff:192.168.100.11
make_gid roceP2p1s0f1 enP2p1s0f1np1 ::ffff:192.168.101.11

cat > "$fixture_dir/bin/ip" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  '-4 route show default') printf 'default via 192.168.1.1 dev wlP9s9 proto dhcp\n' ;;
  'route show default dev '*) ;;
  '-4 -o addr show dev enp1s0f1np1') [[ "$(cat "$DIRECT_FABRIC_TEST_STATE")" == configured ]] && printf '3: enp1s0f1np1    inet 192.168.100.11/24 scope global\n' ;;
  '-4 -o addr show dev enP2p1s0f1np1') [[ "$(cat "$DIRECT_FABRIC_TEST_STATE")" == configured ]] && printf '4: enP2p1s0f1np1    inet 192.168.101.11/24 scope global\n' ;;
  'route get 192.168.100.10') printf '192.168.100.10 dev enp1s0f1np1 src 192.168.100.11\n' ;;
  'route get 192.168.101.10') printf '192.168.101.10 dev enP2p1s0f1np1 src 192.168.101.11\n' ;;
  *) printf 'unexpected ip invocation: %s\n' "$*" >&2; exit 99 ;;
esac
EOF

cat > "$fixture_dir/bin/netplan" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  generate) exit "${NETPLAN_GENERATE_RC:-0}" ;;
  try)
    if [[ "${NETPLAN_TRY_RC:-0}" != 0 ]]; then exit "$NETPLAN_TRY_RC"; fi
    if [[ -e "$DIRECT_FABRIC_NETPLAN_DIRECTORY/99-vonk-node-direct-fabric.yaml" ]]; then
      printf 'configured\n' > "$DIRECT_FABRIC_TEST_STATE"
    else
      printf 'baseline\n' > "$DIRECT_FABRIC_TEST_STATE"
    fi
    ;;
  apply)
    if [[ -e "$DIRECT_FABRIC_NETPLAN_DIRECTORY/99-vonk-node-direct-fabric.yaml" ]]; then
      printf 'configured\n' > "$DIRECT_FABRIC_TEST_STATE"
    else
      printf 'baseline\n' > "$DIRECT_FABRIC_TEST_STATE"
    fi
    ;;
  *) printf 'unexpected netplan invocation: %s\n' "$*" >&2; exit 99 ;;
esac
EOF
chmod +x "$fixture_dir/bin/ip" "$fixture_dir/bin/netplan"

run_script() {
  PATH="$fixture_dir/bin:$PATH" \
    DIRECT_FABRIC_TEST_ALLOW_UNPRIVILEGED=1 \
    DIRECT_FABRIC_NETPLAN_DIRECTORY="$netplan_dir" \
    DIRECT_FABRIC_SYS_CLASS_NET="$sys_net" \
    DIRECT_FABRIC_SYS_CLASS_INFINIBAND="$sys_ib" \
    DIRECT_FABRIC_ROLLBACK_DIRECTORY="$rollback_dir" \
    DIRECT_FABRIC_TEST_STATE="$state_file" \
    "$script" --node node2 "$@"
}

managed_plan="$netplan_dir/99-vonk-node-direct-fabric.yaml"
if run_script --apply > "$fixture_dir/apply-retired.out" 2>&1; then
  printf 'retired manual apply path was accepted\n' >&2
  exit 1
fi
test ! -e "$managed_plan"
test "$(cat "$state_file")" = baseline
grep -Fq -- '--apply is retired; use NVIDIA Sync Cluster Assistant' \
  "$fixture_dir/apply-retired.out"

run_script --emit-netplan > "$managed_plan"
printf 'configured\n' > "$state_file"
run_script --local-postcheck > "$fixture_dir/local.out"
grep -Fq 'PASS: local fabric validation passed for node2' "$fixture_dir/local.out"

if NETPLAN_TRY_RC=8 run_script --rollback > "$fixture_dir/rollback-try-fail.out" 2>&1; then
  printf 'rollback accepted a rejected netplan try\n' >&2
  exit 1
fi
test -f "$managed_plan"
test "$(cat "$state_file")" = configured
grep -Fq 'restored previous Netplan and runtime state' "$fixture_dir/rollback-try-fail.out"

run_script --rollback > "$fixture_dir/rollback.out"
test ! -e "$managed_plan"
test "$(cat "$state_file")" = baseline
grep -Fq 'PASS: manual fabric plan removed' "$fixture_dir/rollback.out"

run_script --emit-netplan > "$managed_plan"
printf 'configured\n' > "$state_file"

printf 'wrong-interface\n' > "$sys_ib/rocep1s0f1/ports/1/gid_attrs/ndevs/3"
if run_script --local-postcheck > "$fixture_dir/gid-binding.out" 2>&1; then
  printf 'local validation accepted a GID bound to the wrong netdev\n' >&2
  exit 1
fi
grep -Fq 'no RoCE v2 GID maps rocep1s0f1/enp1s0f1np1 to 192.168.100.11' \
  "$fixture_dir/gid-binding.out"

cat > "$netplan_dir/99-dgx-spark-direct-fabric.yaml" <<'EOF'
network:
  version: 2
  ethernets:
    enp1s0f1np1:
      addresses: [192.168.100.11/24]
EOF
if run_script --check > "$fixture_dir/foreign-owner.out" 2>&1; then
  printf 'preflight accepted a foreign Netplan owner for a fabric interface\n' >&2
  exit 1
fi
grep -Fq 'foreign Netplan claims fabric interface enp1s0f1np1' \
  "$fixture_dir/foreign-owner.out"

printf 'direct fabric transaction and binding safeguards: PASS\n'

#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
harness=$root/test_agent_upgrade_repair_systemd.sh
mode=${REPAIR_MATRIX_MODE:-minimal}

case "$mode" in
  minimal)
    phases=(none pre-runner-rename post-runner-rename armed installing \
      helper-proven helper-proven-boot)
    faults=(wrong-node config-mode direct-dpkg dpkg-iU installed-agent \
      installed-agent-unit running-helper cgroup-helper source-intent \
      source-cache source-runner source-gate source-lock-busy)
    ;;
  full)
    phases=(none pre-runner-rename post-runner-rename armed installing configured \
      helper-proven helper-proven-boot agent-proven agent-proven-boot)
    faults=(wrong-node config-mode config-symlink direct-dpkg \
      dpkg-iU dpkg-iF dpkg-iHR absent newer \
      installed-agent installed-helper installed-agent-unit \
      installed-helper-unit installed-socket-unit \
      running-agent running-helper cgroup-agent cgroup-helper \
      source-intent source-cache source-runner source-unit source-gate \
      source-dropin source-blocker source-pending source-lock-busy)
    ;;
  *) printf 'unknown repair matrix mode: %s\n' "$mode" >&2; exit 64 ;;
esac

run_fixture() {
  local label=$1 started=$SECONDS
  shift
  printf 'node repair matrix phase: %s\n' "$label"
  env "$@" "$harness"
  printf 'node repair matrix phase: %s PASS (%ss)\n' \
    "$label" "$((SECONDS - started))"
}

for phase in "${phases[@]}"; do
  run_fixture "crash=$phase" "REPAIR_CRASH_PHASE=$phase"
done
run_fixture standard-residue=exact-0755 REPAIR_STANDARD_RESIDUE=exact-0755
for fault in "${faults[@]}"; do
  run_fixture "fault=$fault" "REPAIR_FAULT=$fault"
done

printf 'node repair %s native matrix: PASS\n' "$mode"

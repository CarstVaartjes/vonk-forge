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

for phase in "${phases[@]}"; do
  REPAIR_CRASH_PHASE=$phase "$harness"
done
for fault in "${faults[@]}"; do
  REPAIR_FAULT=$fault "$harness"
done

printf 'node repair %s native matrix: PASS\n' "$mode"

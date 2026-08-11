#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runbook="${FABRIC_RUNBOOK:-$repo_root/docs/runbooks/fabric.md}"

grep -Fq 'scp -o ForwardAgent=no nodes/bin/configure-direct-fabric' "$runbook"
grep -Fq 'sudo bash /tmp/configure-direct-fabric --node node2 --local-postcheck' "$runbook"
grep -Fq 'sudo cat /etc/netplan/99-vonk-node-direct-fabric.yaml' "$runbook"
grep -Fq 'set -euo pipefail' "$runbook"
grep -Fq 'NVIDIA `dgx-spark-playbooks` commit' "$runbook"
grep -Fq "prefer NVIDIA Sync's Cluster Assistant" "$runbook"
grep -Fq 'do not layer it on top' "$runbook"
grep -Fq 'of Sync-managed Netplan' "$runbook"
grep -Fq 'nodes/bin/rollback-direct-fabric' "$runbook"
grep -Fq 'GPU node 2 is a hard' "$runbook"

worker_validation_line="$(grep -n 'node2 --local-postcheck' "$runbook" | head -n1 | cut -d: -f1)"
head_stage_line="$(grep -n 'vonk-node-1:/tmp/configure-direct-fabric' "$runbook" | head -n1 | cut -d: -f1)"
test "$worker_validation_line" -lt "$head_stage_line"

printf 'fabric runbook safety invariants: PASS\n'

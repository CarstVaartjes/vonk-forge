#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runbook="${FABRIC_RUNBOOK:-$repo_root/docs/runbooks/fabric.md}"

grep -Fq 'NVIDIA Sync owns fresh fabric configuration' "$runbook"
grep -Fq -- '--apply` is retired and refuses' "$runbook"
if grep -Fq -- '--node node2 --apply' "$runbook"; then
  printf 'runbook still offers the retired manual fabric installation command\n' >&2
  exit 1
fi
grep -Fq 'set -euo pipefail' "$runbook"
grep -Fq 'NVIDIA `dgx-spark-playbooks` commit' "$runbook"
grep -Fq 'Cluster Assistant' "$runbook"
grep -Fq '/etc/netplan/99-dgx-spark-direct-fabric.yaml' "$runbook"
grep -Fq 'must refuse to add its own file' "$runbook"
grep -Fq 'nodes/bin/rollback-direct-fabric' "$runbook"
grep -Fq 'GPU node 2 is a hard' "$runbook"

printf 'fabric runbook safety invariants: PASS\n'

#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/packaging/bin/vonk-agent-upgrade"
test_root="$(mktemp -d)"
trap 'rm -rf -- "$test_root"' EXIT

root="$test_root/root"
bin="$test_root/bin"
log="$test_root/actions.log"
mkdir -p "$root/usr/lib/vonk-forge" "$bin"

write_agent() {
  local version="$1"
  local healthy="$2"
  local target="$root/usr/lib/vonk-forge/vonk-agent"
  local staged="$target.new"
  {
    printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail'
    printf 'version=%q\n' "$version"
    printf 'healthy=%q\n' "$healthy"
    cat <<'AGENT'
case "${1:-}" in
  *) printf 'agent %s\n' "$*" >> "${UPGRADE_ACTION_LOG:?}" ;;
esac
case "${1:-}" in
  --version) printf 'vonk-agent %s\n' "$version" ;;
  self-test) [[ "$healthy" == yes ]] ;;
  *) exit 2 ;;
esac
AGENT
  } > "$staged"
  chmod +x "$staged"
  mv -f "$staged" "$target"
}

write_agent 1.0.0 yes

cat > "$bin/apt-get" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf 'apt-get %s\n' "$*" >> "${UPGRADE_ACTION_LOG:?}"
if [[ "$*" == 'install --only-upgrade --yes vonk-forge-agent' ]]; then
  "${WRITE_UPGRADED_AGENT:?}" 1.1.0 "${UPGRADED_HEALTH:-yes}"
fi
FAKE
cat > "$bin/systemctl" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl %s\n' "$*" >> "${UPGRADE_ACTION_LOG:?}"
if [[ "${1:-}" == is-active && "${2:-}" == --quiet ]]; then
  [[ "${3:-}" == vonk-forge-agent.service ]]
fi
FAKE
cat > "$bin/write-upgraded-agent" <<FAKE
#!/usr/bin/env bash
set -euo pipefail
$(declare -f write_agent)
root=$(printf '%q' "$root")
write_agent "\$1" "\$2"
FAKE
chmod +x "$bin/apt-get" "$bin/systemctl" "$bin/write-upgraded-agent"

UPGRADE_ACTION_LOG="$log" \
VONK_AGENT_UPGRADE_ROOT="$root" \
VONK_AGENT_UPGRADE_TEST_MODE=1 \
VONK_AGENT_UPGRADE_APT_GET="$bin/apt-get" \
VONK_AGENT_UPGRADE_SYSTEMCTL="$bin/systemctl" \
VONK_AGENT_UPGRADE_AGENT="$root/usr/lib/vonk-forge/vonk-agent" \
WRITE_UPGRADED_AGENT="$bin/write-upgraded-agent" \
  "$script" > "$test_root/output"

grep -Fxq 'apt-get update' "$log"
grep -Fxq 'apt-get install --only-upgrade --yes vonk-forge-agent' "$log"
grep -Fxq 'systemctl restart vonk-forge-agent.service' "$log"
grep -Fxq 'systemctl is-active --quiet vonk-forge-agent.service' "$log"
grep -Fxq 'agent self-test' "$log"
grep -Fxq 'agent --version' "$log"
grep -Fq 'upgrade complete: vonk-agent 1.1.0 is healthy' "$test_root/output"
! grep -Eiq 'supervisor|slot|activate|bootstrap' "$log" "$test_root/output"

write_agent 1.0.0 yes
if UPGRADE_ACTION_LOG="$log" \
  VONK_AGENT_UPGRADE_ROOT="$root" \
  VONK_AGENT_UPGRADE_TEST_MODE=1 \
  VONK_AGENT_UPGRADE_APT_GET="$bin/apt-get" \
  VONK_AGENT_UPGRADE_SYSTEMCTL="$bin/systemctl" \
  VONK_AGENT_UPGRADE_AGENT="$root/usr/lib/vonk-forge/vonk-agent" \
  WRITE_UPGRADED_AGENT="$bin/write-upgraded-agent" \
  UPGRADED_HEALTH=no \
    "$script" > "$test_root/unhealthy-output" 2>&1
then
  printf '%s\n' 'upgrade accepted an unhealthy direct agent' >&2
  exit 1
fi
! grep -Fq 'upgrade complete' "$test_root/unhealthy-output"

printf 'vonk-agent direct upgrade wrapper: PASS\n'

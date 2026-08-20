#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/packaging/bin/vonk-agent-upgrade"
test_root="$(mktemp -d)"
trap 'rm -rf -- "$test_root"' EXIT

root="$test_root/root"
bin="$test_root/bin"
log="$test_root/actions.log"
mkdir -p "$root/usr/lib/vonk-forge" "$root/etc/vonk-forge-agent" "$bin"
: > "$root/etc/vonk-forge-agent/agent.toml"

write_agent() {
  local version="$1"
  local receipt_health="$2"
  local target="$root/usr/lib/vonk-forge/vonk-agent"
  local staged="$target.new"
  {
    printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail'
    printf 'version=%q\n' "$version"
    printf 'receipt_health=%q\n' "$receipt_health"
    cat <<'AGENT'
printf 'agent %s\n' "$*" >> "${UPGRADE_ACTION_LOG:?}"
case "${1:-}" in
  --version)
    printf 'vonk-agent %s\n' "$version"
    ;;
  --config)
    case "${3:-}" in
      self-test) printf '{"self_test_passed":true}\n' ;;
      verify-readiness) [[ "$receipt_health" == yes ]] ;;
      *) exit 2 ;;
    esac
    ;;
  *) exit 2 ;;
esac
AGENT
  } > "$staged"
  chmod +x "$staged"
  mv -f "$staged" "$target"
}

cat > "$bin/apt-get" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf 'apt-get %s\n' "$*" >> "${UPGRADE_ACTION_LOG:?}"
if [[ "$*" == 'install --only-upgrade --yes vonk-forge-agent' ]]; then
  "${WRITE_UPGRADED_AGENT:?}" "${UPGRADED_VERSION:-1.1.0}" "${UPGRADED_HEALTH:-yes}"
fi
FAKE
cat > "$bin/dpkg" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf 'dpkg %s\n' "$*" >> "${UPGRADE_ACTION_LOG:?}"
[[ "$*" == '--configure -a' ]]
FAKE
cat > "$bin/dpkg-query" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf 'dpkg-query %s\n' "$*" >> "${UPGRADE_ACTION_LOG:?}"
printf '%s\n' "${PACKAGE_VERSION:-1.1.0+build.7}"
FAKE
cat > "$bin/systemctl" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl %s\n' "$*" >> "${UPGRADE_ACTION_LOG:?}"
case "${1:-}" in
  restart) [[ "${2:-}" == vonk-forge-agent.service ]] ;;
  is-active) [[ "${2:-}" == --quiet && "${3:-}" == vonk-forge-agent.service ]] ;;
  show)
    [[ "$*" == 'show --property MainPID --value vonk-forge-agent.service' ]]
    if [[ "${CHURN_PID:-no}" == yes ]]; then
      count=$(cat "${SYSTEMCTL_STATE:?}")
      (( count += 1 ))
      printf '%s\n' "$count" > "${SYSTEMCTL_STATE:?}"
      printf '%s\n' "$(( 4200 + count ))"
    else
      printf '4242\n'
    fi
    ;;
  *) exit 2 ;;
esac
FAKE
cat > "$bin/write-upgraded-agent" <<FAKE
#!/usr/bin/env bash
set -euo pipefail
$(declare -f write_agent)
root=$(printf '%q' "$root")
write_agent "\$1" "\$2"
FAKE
chmod +x "$bin/apt-get" "$bin/dpkg" "$bin/dpkg-query" "$bin/systemctl" \
  "$bin/write-upgraded-agent"

common_environment=(
  UPGRADE_ACTION_LOG="$log"
  VONK_AGENT_UPGRADE_ROOT="$root"
  VONK_AGENT_UPGRADE_TEST_MODE=1
  VONK_AGENT_UPGRADE_APT_GET="$bin/apt-get"
  VONK_AGENT_UPGRADE_DPKG="$bin/dpkg"
  VONK_AGENT_UPGRADE_DPKG_QUERY="$bin/dpkg-query"
  VONK_AGENT_UPGRADE_SYSTEMCTL="$bin/systemctl"
  VONK_AGENT_UPGRADE_AGENT="$root/usr/lib/vonk-forge/vonk-agent"
  VONK_AGENT_UPGRADE_CONFIG="$root/etc/vonk-forge-agent/agent.toml"
  VONK_AGENT_UPGRADE_RECEIPT="$root/run/vonk-forge-agent/readiness.json"
  VONK_AGENT_UPGRADE_ATTEMPTS=4
  VONK_AGENT_UPGRADE_SUSTAINED=3
  VONK_AGENT_UPGRADE_INTERVAL_SECONDS=0
  WRITE_UPGRADED_AGENT="$bin/write-upgraded-agent"
  SYSTEMCTL_STATE="$test_root/systemctl-state"
)
: > "$test_root/systemctl-state"

# There is intentionally no old agent: package repair must happen before the
# wrapper requires the atomically installed replacement.
env "${common_environment[@]}" "$script" > "$test_root/output"

test "$(sed -n '/apt-get --fix-broken install --yes/=' "$log")" -lt \
  "$(sed -n '/apt-get install --only-upgrade --yes/=' "$log")"
test "$(sed -n '/dpkg --configure -a/=' "$log")" -lt \
  "$(sed -n '/apt-get install --only-upgrade --yes/=' "$log")"
grep -Fxq 'apt-get update' "$log"
grep -Fxq 'systemctl restart vonk-forge-agent.service' "$log"
test "$(grep -Fc 'systemctl is-active --quiet vonk-forge-agent.service' "$log")" = 3
test "$(grep -Fc 'agent --config ' "$log")" = 4
grep -Fq ' self-test' "$log"
test "$(grep -Fc ' verify-readiness ' "$log")" = 3
grep -Fq 'upgrade complete: vonk-agent 1.1.0 is healthy' "$test_root/output"
! grep -Eiq 'supervisor|slot|activate|bootstrap' "$log" "$test_root/output"

if env "${common_environment[@]}" UPGRADED_HEALTH=no \
  "$script" > "$test_root/unhealthy-output" 2>&1
then
  printf '%s\n' 'upgrade accepted an agent without controller readiness' >&2
  exit 1
fi
grep -Fq 'controller readiness receipt was not sustained' \
  "$test_root/unhealthy-output"
! grep -Fq 'upgrade complete' "$test_root/unhealthy-output"

: > "$test_root/systemctl-state"
if env "${common_environment[@]}" CHURN_PID=yes \
  "$script" > "$test_root/churn-output" 2>&1
then
  printf '%s\n' 'upgrade accepted health across different service processes' >&2
  exit 1
fi
grep -Fq 'controller readiness receipt was not sustained' "$test_root/churn-output"

if env "${common_environment[@]}" PACKAGE_VERSION=1.2.0 \
  "$script" > "$test_root/version-output" 2>&1
then
  printf '%s\n' 'upgrade accepted a package/binary semantic mismatch' >&2
  exit 1
fi
grep -Fq 'does not match package semantic version 1.2.0' \
  "$test_root/version-output"

printf 'vonk-agent direct upgrade wrapper: PASS\n'

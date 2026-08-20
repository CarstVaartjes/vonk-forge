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
if [[ "$*" == 'install --only-upgrade --yes vonk-forge-agent' \
  || "$*" == install\ --yes\ --no-install-recommends\ -o\ Dpkg::Options::=--force-confold\ /*.deb ]]; then
  "${WRITE_UPGRADED_AGENT:?}" "${UPGRADED_VERSION:-1.1.0}" "${UPGRADED_HEALTH:-yes}"
fi
FAKE
cat > "$bin/dpkg" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf 'dpkg %s\n' "$*" >> "${UPGRADE_ACTION_LOG:?}"
case "$*" in
  '--configure -a') ;;
  '--install '*)
    "${WRITE_UPGRADED_AGENT:?}" "${UPGRADED_VERSION:-1.1.0}" "${UPGRADED_HEALTH:-yes}"
    ;;
  *) exit 2 ;;
esac
FAKE
cat > "$bin/dpkg-query" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf 'dpkg-query %s\n' "$*" >> "${UPGRADE_ACTION_LOG:?}"
if [[ "$*" == *'${db:Status-Status}'* ]]; then
  [[ "${PACKAGE_NOT_INSTALLED:-no}" != yes ]]
  printf 'installed\n'
else
  printf '%s\n' "${PACKAGE_VERSION:-1.1.0+build.7}"
fi
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
env "${common_environment[@]}" "$script" apt > "$test_root/output"

test "$(sed -n '/apt-get --fix-broken install --yes/=' "$log")" -lt \
  "$(sed -n '/apt-get install --only-upgrade --yes/=' "$log")"
test "$(sed -n '/dpkg --configure -a/=' "$log")" -lt \
  "$(sed -n '/apt-get install --only-upgrade --yes/=' "$log")"
grep -Fxq 'apt-get update' "$log"
! grep -Fq 'systemctl restart' "$log"
test "$(grep -Fc 'systemctl is-active --quiet vonk-forge-agent.service' "$log")" = 3
test "$(grep -Fc 'agent --config ' "$log")" = 4
grep -Fq ' self-test' "$log"
test "$(grep -Fc ' verify-readiness ' "$log")" = 3
grep -Fq 'upgrade complete: vonk-agent 1.1.0 is healthy' "$test_root/output"
! grep -Eiq 'supervisor|slot|activate|bootstrap' "$log" "$test_root/output"

if env "${common_environment[@]}" UPGRADED_HEALTH=no \
  "$script" apt > "$test_root/unhealthy-output" 2>&1
then
  printf '%s\n' 'upgrade accepted an agent without controller readiness' >&2
  exit 1
fi
grep -Fq 'controller readiness receipt was not sustained' \
  "$test_root/unhealthy-output"
! grep -Fq 'upgrade complete' "$test_root/unhealthy-output"

: > "$test_root/systemctl-state"
if env "${common_environment[@]}" CHURN_PID=yes \
  "$script" apt > "$test_root/churn-output" 2>&1
then
  printf '%s\n' 'upgrade accepted health across different service processes' >&2
  exit 1
fi
grep -Fq 'controller readiness receipt was not sustained' "$test_root/churn-output"

if env "${common_environment[@]}" PACKAGE_VERSION=1.2.0 \
  "$script" apt > "$test_root/version-output" 2>&1
then
  printf '%s\n' 'upgrade accepted a package/binary semantic mismatch' >&2
  exit 1
fi
grep -Fq 'does not match package semantic version 1.2.0' \
  "$test_root/version-output"

# The curl-installer trust boundary is explicit: download and signature
# verification happen as the caller, then this root phase accepts only a local
# package and performs no network or package-index repair.
: > "$log"
local_package="$test_root/vonk-forge-agent_1.1.0_arm64.deb"
: > "$local_package"
local_sha256=$(sha256sum "$local_package" | cut -d' ' -f1)
env "${common_environment[@]}" "$script" install-local "$local_package" \
  "$local_sha256" \
  > "$test_root/local-output"
grep -Fxq 'apt-get update' "$log"
grep -Fxq "apt-get install --yes --no-install-recommends -o Dpkg::Options::=--force-confold $local_package" "$log"
! grep -Fq "dpkg --install $local_package" "$log"
grep -Fq 'upgrade complete: vonk-agent 1.1.0 is healthy' \
  "$test_root/local-output"

# Controller unavailability is reported only after dpkg has successfully
# installed/configured the verified package, so package-manager state remains
# repairable and is never poisoned by a maintainer-script network gate.
: > "$log"
if env "${common_environment[@]}" UPGRADED_HEALTH=no \
  "$script" install-local "$local_package" "$local_sha256" \
  > "$test_root/local-controller-down-output" 2>&1
then
  printf '%s\n' 'local upgrade accepted unavailable controller readiness' >&2
  exit 1
fi
grep -Fxq "apt-get install --yes --no-install-recommends -o Dpkg::Options::=--force-confold $local_package" "$log"
test "$(UPGRADE_ACTION_LOG="$log" \
  "$root/usr/lib/vonk-forge/vonk-agent" --version)" = \
  'vonk-agent 1.1.0'
grep -Fq 'controller readiness receipt was not sustained' \
  "$test_root/local-controller-down-output"

if env "${common_environment[@]}" "$script" install-local \
  'https://packages.invalid/agent.deb' "$local_sha256" \
  > "$test_root/url-output" 2>&1
then
  printf '%s\n' 'local install accepted a network URL' >&2
  exit 1
fi
grep -Fq 'local package' "$test_root/url-output"

if env "${common_environment[@]}" "$script" install-local "$local_package" \
  "$(printf 'f%.0s' {1..64})" > "$test_root/digest-output" 2>&1
then
  printf '%s\n' 'local install accepted bytes outside the caller-verified digest' >&2
  exit 1
fi
grep -Fq 'SHA-256 changed' "$test_root/digest-output"

: > "$log"
env "${common_environment[@]}" PACKAGE_NOT_INSTALLED=yes \
  "$script" install-local "$local_package" "$local_sha256" \
  > "$test_root/fresh-output"
grep -Fq 'pair the agent before starting the service' "$test_root/fresh-output"
! grep -Eq '^systemctl ' "$log"
! grep -Fq 'self-test' "$log"
! grep -Fq 'verify-readiness' "$log"

if env "${common_environment[@]}" "$script" > "$test_root/usage-output" 2>&1
then
  printf '%s\n' 'upgrade wrapper accepted an ambiguous default mode' >&2
  exit 1
fi
grep -Fq 'install-local' "$test_root/usage-output"
grep -Fq 'apt' "$test_root/usage-output"

printf 'vonk-agent direct upgrade wrapper: PASS\n'

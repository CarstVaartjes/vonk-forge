#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
helper="$repo_root/scripts/test-agent-package-native-lifecycle"
test_root="$(mktemp -d)"
trap 'rm -rf -- "$test_root"' EXIT

root="$test_root/root"
bin="$test_root/bin"
log="$test_root/actions.log"
state="$test_root/package-state"
version_state="$test_root/package-version"
mkdir -p "$bin" "$root/etc/vonk-forge-agent" \
  "$root/usr/lib/vonk-forge" "$root/var/lib/vonk-forge-agent"
: > "$log"
printf 'absent\n' > "$state"
printf 'absent\n' > "$version_state"

baseline_version=0.0.0~acceptance.1+g0123456789ab
candidate_version=0.1.0
current="$test_root/vonk-forge-agent_${baseline_version}_amd64.deb"
next="$test_root/vonk-forge-agent_${candidate_version}_amd64.deb"
: > "$current"
: > "$current.sha256"
: > "$next"
: > "$next.sha256"

cat > "$root/usr/lib/vonk-forge/vonk-agent" <<'AGENT'
#!/usr/bin/env bash
set -euo pipefail
printf 'agent %s\n' "$*" >> "${LIFECYCLE_ACTION_LOG:?}"
case "${1:-}" in
  --version)
    semantic=$(cat "${LIFECYCLE_VERSION_STATE:?}")
    semantic=${semantic%%~*}
    semantic=${semantic%%+*}
    printf 'version %s\n' "$semantic" >> "${LIFECYCLE_ACTION_LOG:?}"
    printf 'vonk-agent %s\n' "$semantic"
    ;;
  --config)
    [[ "${3:-}" == self-test ]]
    semantic=$(cat "${LIFECYCLE_VERSION_STATE:?}")
    semantic=${semantic%%~*}
    semantic=${semantic%%+*}
    printf 'self-test %s\n' "$semantic" >> "${LIFECYCLE_ACTION_LOG:?}"
    if [[ "$semantic" == 0.0.0 ]]; then
      build=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      binary=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
    else
      build=dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
      binary=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
    fi
    printf '{"semantic_version":"%s","build_digest":"sha256:%s","binary_digest":"%s","architecture":"linux-amd64","self_test_passed":true}\n' "$semantic" "$build" "$binary"
    ;;
  *) exit 2 ;;
esac
AGENT
chmod +x "$root/usr/lib/vonk-forge/vonk-agent"

cat > "$bin/verify-agent-deb" <<'VERIFY'
#!/usr/bin/env bash
set -euo pipefail
printf 'verify %s\n' "$*" >> "${LIFECYCLE_ACTION_LOG:?}"
VERIFY

cat > "$bin/dpkg-deb" <<'DPKG_DEB'
#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == --field ]]
package=$2
field=$3
case "$field" in
  Architecture) printf 'amd64\n' ;;
  Version)
    name=${package##*/}
    version=${name#vonk-forge-agent_}
    printf '%s\n' "${version%_amd64.deb}"
    ;;
  *) exit 2 ;;
esac
DPKG_DEB

cat > "$bin/dpkg-query" <<'DPKG_QUERY'
#!/usr/bin/env bash
set -euo pipefail
printf 'query %s\n' "$*" >> "${LIFECYCLE_ACTION_LOG:?}"
case "$*" in
  *'${db:Status-Abbrev}'*) cat "${LIFECYCLE_STATE:?}" ;;
  *'${Version}'*) cat "${LIFECYCLE_VERSION_STATE:?}" ;;
  *) exit 2 ;;
esac
DPKG_QUERY

cat > "$bin/dpkg" <<'DPKG'
#!/usr/bin/env bash
set -euo pipefail
printf 'dpkg %s\n' "$*" >> "${LIFECYCLE_ACTION_LOG:?}"
case "${1:-}" in
  --compare-versions)
    /usr/bin/dpkg --compare-versions "$2" "$3" "$4"
    ;;
  -i|--install)
    package=${2##*/}
    candidate=${package#vonk-forge-agent_}
    candidate=${candidate%_amd64.deb}
    installed=$(cat "${LIFECYCLE_VERSION_STATE:?}")
    if [[ "$installed" == 0.1.0 && "$candidate" == 0.0.0~acceptance.1+g0123456789ab ]]; then
      printf '%s\n' 'vonk-forge-agent: refusing downgrade' >&2
      exit 1
    fi
    printf '%s\n' "$candidate" > "${LIFECYCLE_VERSION_STATE:?}"
    printf 'ii \n' > "${LIFECYCLE_STATE:?}"
    ;;
  --unpack)
    package=${2##*/}
    candidate=${package#vonk-forge-agent_}
    candidate=${candidate%_amd64.deb}
    printf '%s\n' "$candidate" > "${LIFECYCLE_VERSION_STATE:?}"
    printf 'iU \n' > "${LIFECYCLE_STATE:?}"
    ;;
  --configure) printf 'ii \n' > "${LIFECYCLE_STATE:?}" ;;
  --remove) printf 'absent\n' > "${LIFECYCLE_STATE:?}" ;;
  *) exit 2 ;;
esac
DPKG

cat > "$bin/systemd-analyze" <<'SYSTEMD'
#!/usr/bin/env bash
set -euo pipefail
printf 'systemd-analyze %s\n' "$*" >> "${LIFECYCLE_ACTION_LOG:?}"
SYSTEMD

cat > "$bin/runuser" <<'RUNUSER'
#!/usr/bin/env bash
set -euo pipefail
printf 'runuser %s\n' "$*" >> "${LIFECYCLE_ACTION_LOG:?}"
printf 'true\n'
RUNUSER
chmod +x "$bin"/*

env \
  LIFECYCLE_ACTION_LOG="$log" \
  LIFECYCLE_STATE="$state" \
  LIFECYCLE_VERSION_STATE="$version_state" \
  VONK_AGENT_LIFECYCLE_AGENT="$root/usr/lib/vonk-forge/vonk-agent" \
  VONK_AGENT_LIFECYCLE_DPKG="$bin/dpkg" \
  VONK_AGENT_LIFECYCLE_DPKG_DEB="$bin/dpkg-deb" \
  VONK_AGENT_LIFECYCLE_DPKG_QUERY="$bin/dpkg-query" \
  VONK_AGENT_LIFECYCLE_DOWNGRADE_LOG="$test_root/downgrade.log" \
  VONK_AGENT_LIFECYCLE_MACHINE=x86_64 \
  VONK_AGENT_LIFECYCLE_ROOT="$root" \
  VONK_AGENT_LIFECYCLE_RUNUSER="$bin/runuser" \
  VONK_AGENT_LIFECYCLE_SYSTEMD_ANALYZE="$bin/systemd-analyze" \
  VONK_AGENT_LIFECYCLE_TEST_MODE=1 \
  VONK_AGENT_LIFECYCLE_VERIFY="$bin/verify-agent-deb" \
  "$helper" linux-amd64 "$current" "$next" "$baseline_version" "$candidate_version"

test "$(grep -Fc 'verify --json' "$log")" = 2
grep -Fxq "dpkg -i $current" "$log"
grep -Fxq "dpkg --unpack $next" "$log"
grep -Fxq 'dpkg --configure -a' "$log"
grep -Fxq "dpkg -i $next" "$log"
test "$(grep -Fc 'dpkg --remove vonk-forge-agent' "$log")" = 2
grep -Fxq 'version 0.0.0' "$log"
grep -Fxq 'self-test 0.0.0' "$log"
grep -Fxq 'version 0.1.0' "$log"
test "$(grep -Fc 'self-test 0.1.0' "$log")" = 2
grep -Fq 'refusing downgrade' "$test_root/downgrade.log"
grep -Fxq '# lifecycle-preserved' "$root/etc/vonk-forge-agent/agent.toml"
grep -Fxq 'enrollment_url = "https://127.0.0.1:9/"' \
  "$root/etc/vonk-forge-agent/agent.toml"
grep -Fxq "ca_path = \"$root/etc/vonk-forge-agent/controller-ca.pem\"" \
  "$root/etc/vonk-forge-agent/agent.toml"
grep -Fxq "data_dir = \"$root/var/lib/vonk-forge-agent\"" \
  "$root/etc/vonk-forge-agent/agent.toml"
test -f "$root/var/lib/vonk-forge-agent/lifecycle-preserved"
test "$(/usr/bin/openssl x509 \
  -in "$root/etc/vonk-forge-agent/controller-ca.pem" -noout -text \
  | grep -Fc 'X509v3 Basic Constraints: critical')" = 1
test "$(/usr/bin/openssl x509 \
  -in "$root/var/lib/vonk-forge-agent/credentials/certificate.pem" -noout -text \
  | grep -Fc 'TLS Web Client Authentication')" = 1
test "$(cat "$state")" = absent

printf 'native direct-package lifecycle helper: PASS\n'

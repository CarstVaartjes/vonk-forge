#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/packaging/bin/vonk-agent-upgrade"
test_root="$(mktemp -d)"
trap 'rm -rf -- "$test_root"' EXIT

make_fixture() {
  local name="$1" with_bootstrap="$2"
  local root="$test_root/$name"
  mkdir -p "$root/usr/lib/vonk-forge" \
    "$root/var/lib/vonk-forge/slots/b" \
    "$root/var/lib/vonk-forge/supervisor"

  cat > "$root/usr/lib/vonk-forge/vonk-agent-supervisor" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  package-staging-slot)
    printf 'b\n'
    ;;
  activate)
    printf 'activate %s\n' "$*" >> "${UPGRADE_ACTION_LOG:?}"
    slot=''
    while (( $# > 0 )); do
      if [[ "$1" == '--slot' ]]; then slot="$2"; shift 2; else shift; fi
    done
    ln -sfn "../../slots/$slot" \
      "${VONK_AGENT_UPGRADE_ROOT:?}/var/lib/vonk-forge/supervisor/current"
    ;;
  *)
    printf 'unexpected supervisor command: %s\n' "$*" >&2
    exit 99
    ;;
esac
FAKE
  chmod +x "$root/usr/lib/vonk-forge/vonk-agent-supervisor"

  if [[ "$with_bootstrap" == yes ]]; then
    cat > "$root/var/lib/vonk-forge/slots/b/vonk-agent" <<'AGENT'
#!/usr/bin/env bash
if [[ "${1:-}" == '--help' ]]; then
  printf 'Commands:\n  run\n  bootstrap\n  pair\n'
elif [[ "${1:-}" == '--version' ]]; then
  printf 'vonk-agent 1.2.3\n'
fi
AGENT
  else
    cat > "$root/var/lib/vonk-forge/slots/b/vonk-agent" <<'AGENT'
#!/usr/bin/env bash
if [[ "${1:-}" == '--help' ]]; then
  printf 'Commands:\n  run\n  pair\n'
fi
AGENT
  fi
  chmod +x "$root/var/lib/vonk-forge/slots/b/vonk-agent"
  printf '{"status":"stable"}\n' > "$root/var/lib/vonk-forge/supervisor/state.json"
  printf '%s\n' "$root"
}

mkdir -p "$test_root/bin"
cat > "$test_root/bin/apt-get" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${UPGRADE_ACTION_LOG:?}"
FAKE
cat > "$test_root/bin/systemctl" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == is-active && "${2:-}" == --quiet ]]; then exit 0; fi
exit 0
FAKE
chmod +x "$test_root/bin/apt-get" "$test_root/bin/systemctl"

incompatible_root="$(make_fixture incompatible no)"
if PATH="$test_root/bin:$PATH" \
  UPGRADE_ACTION_LOG="$test_root/incompatible.log" \
  VONK_AGENT_UPGRADE_ROOT="$incompatible_root" \
  VONK_AGENT_UPGRADE_TEST_MODE=1 \
  VONK_AGENT_UPGRADE_APT_GET="$test_root/bin/apt-get" \
  VONK_AGENT_UPGRADE_SYSTEMCTL="$test_root/bin/systemctl" \
  "$script" > "$test_root/incompatible.out" 2>&1
then
  printf 'upgrade accepted an agent without bootstrap\n' >&2
  exit 1
fi
grep -Fq 'staged agent does not support bootstrap' "$test_root/incompatible.out" || {
  cat "$test_root/incompatible.out" >&2
  exit 1
}
! grep -Fq 'activate' "$test_root/incompatible.log"

compatible_root="$(make_fixture compatible yes)"
PATH="$test_root/bin:$PATH" \
  UPGRADE_ACTION_LOG="$test_root/compatible.log" \
  VONK_AGENT_UPGRADE_ROOT="$compatible_root" \
  VONK_AGENT_UPGRADE_TEST_MODE=1 \
  VONK_AGENT_UPGRADE_APT_GET="$test_root/bin/apt-get" \
  VONK_AGENT_UPGRADE_SYSTEMCTL="$test_root/bin/systemctl" \
  VONK_AGENT_UPGRADE_POLL_SECONDS=0 \
  "$script" > "$test_root/compatible.out"
grep -Fq 'activate' "$test_root/compatible.log"
grep -Fq 'upgrade complete' "$test_root/compatible.out"

printf 'vonk-agent upgrade wrapper: PASS\n'

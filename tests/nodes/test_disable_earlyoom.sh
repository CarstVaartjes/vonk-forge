#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/nodes/bin/disable-earlyoom"
fixture_dir="$(mktemp -d)"
trap 'rm -rf -- "$fixture_dir"' EXIT

cat > "$fixture_dir/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

state="${EARLYOOM_TEST_STATE:?}"
if [[ -n "${EARLYOOM_TEST_STATE_FILE:-}" ]]; then
  state="$(cat "$EARLYOOM_TEST_STATE_FILE")"
fi

case "$1" in
  stop)
    printf 'stop\n' >> "${EARLYOOM_TEST_ACTION_LOG:?}"
    case "$state" in
      enabled|enabled_active) printf 'enabled_inactive\n' > "$EARLYOOM_TEST_STATE_FILE" ;;
      disabled_active) printf 'disabled\n' > "$EARLYOOM_TEST_STATE_FILE" ;;
      masked_active) printf 'masked\n' > "$EARLYOOM_TEST_STATE_FILE" ;;
      *) printf 'unsupported stop state: %s\n' "$state" >&2; exit 98 ;;
    esac
    exit 0
    ;;
  disable)
    printf 'disable\n' >> "${EARLYOOM_TEST_ACTION_LOG:?}"
    printf 'disabled\n' > "${EARLYOOM_TEST_STATE_FILE:?}"
    exit 0
    ;;
esac

case "$state" in
  absent)
    case "$1" in
      show) printf 'not-found\n'; exit 0 ;;
      is-enabled) printf 'not-found\n'; exit 4 ;;
      is-active) printf 'inactive\n'; exit 4 ;;
    esac
    ;;
  disabled)
    case "$1" in
      show) printf 'loaded\n'; exit 0 ;;
      is-enabled) printf 'disabled\n'; exit 1 ;;
      is-active) printf 'inactive\n'; exit 3 ;;
    esac
    ;;
  masked)
    case "$1" in
      show) printf 'masked\n'; exit 0 ;;
      is-enabled) printf 'masked\n'; exit 1 ;;
      is-active) printf 'inactive\n'; exit 3 ;;
    esac
    ;;
  enabled)
    case "$1" in
      show) printf 'loaded\n'; exit 0 ;;
      is-enabled) printf 'enabled\n'; exit 0 ;;
      is-active) printf 'active\n'; exit 0 ;;
    esac
    ;;
  enabled_active)
    case "$1" in
      show) printf 'loaded\n'; exit 0 ;;
      is-enabled) printf 'enabled\n'; exit 0 ;;
      is-active) printf 'active\n'; exit 0 ;;
    esac
    ;;
  enabled_inactive)
    case "$1" in
      show) printf 'loaded\n'; exit 0 ;;
      is-enabled) printf 'enabled\n'; exit 0 ;;
      is-active) printf 'inactive\n'; exit 3 ;;
    esac
    ;;
  disabled_active)
    case "$1" in
      show) printf 'loaded\n'; exit 0 ;;
      is-enabled) printf 'disabled\n'; exit 1 ;;
      is-active) printf 'active\n'; exit 0 ;;
    esac
    ;;
  masked_active)
    case "$1" in
      show) printf 'masked\n'; exit 0 ;;
      is-enabled) printf 'masked\n'; exit 1 ;;
      is-active) printf 'active\n'; exit 0 ;;
    esac
    ;;
  static)
    case "$1" in
      show) printf 'loaded\n'; exit 0 ;;
      is-enabled) printf 'static\n'; exit 0 ;;
      is-active) printf 'inactive\n'; exit 3 ;;
    esac
    ;;
  invalid_enabled_rc)
    case "$1" in
      show) printf 'loaded\n'; exit 0 ;;
      is-enabled) printf 'disabled\n'; exit 0 ;;
      is-active) printf 'inactive\n'; exit 3 ;;
    esac
    ;;
  invalid_active_rc)
    case "$1" in
      show) printf 'loaded\n'; exit 0 ;;
      is-enabled) printf 'disabled\n'; exit 1 ;;
      is-active) printf 'inactive\n'; exit 0 ;;
    esac
    ;;
esac

printf 'unexpected systemctl invocation: %s\n' "$*" >&2
exit 99
EOF

cat > "$fixture_dir/dpkg-query" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

state="${EARLYOOM_TEST_STATE:?}"
if [[ -n "${EARLYOOM_TEST_STATE_FILE:-}" ]]; then
  state="$(cat "$EARLYOOM_TEST_STATE_FILE")"
fi
if [[ "$state" == absent ]]; then
  printf 'dpkg-query: no packages found matching earlyoom\n' >&2
  exit 1
fi
printf 'installed 1.8.2-1\n'
EOF

chmod +x "$fixture_dir/systemctl" "$fixture_dir/dpkg-query"

run_check() {
  local state="$1"
  local expected_rc="$2"
  local expected_classification="$3"
  local output_file="$fixture_dir/$state.out"
  local rc

  set +e
  PATH="$fixture_dir:$PATH" EARLYOOM_TEST_STATE="$state" \
    bash "$script" --check > "$output_file" 2>&1
  rc=$?
  set -e
  test "$rc" -eq "$expected_rc"
  grep -Fq "classification=$expected_classification" "$output_file"
}

run_check absent 0 absent
grep -Fq 'before.enabled.exit_code=4' "$fixture_dir/absent.out"
grep -Fq 'before.active.exit_code=4' "$fixture_dir/absent.out"
grep -Fq 'PASS: earlyoom is absent; no change required' "$fixture_dir/absent.out"

run_check disabled 0 disabled
grep -Fq 'PASS: earlyoom is disabled and inactive' "$fixture_dir/disabled.out"

run_check masked 0 masked
grep -Fq 'PASS: earlyoom is masked and inactive' "$fixture_dir/masked.out"

run_check enabled 2 change_required_enabled
grep -Fq 'CHANGE_REQUIRED: earlyoom must be made inactive and non-enabled' \
  "$fixture_dir/enabled.out"

run_check disabled_active 2 change_required_disabled_active
run_check masked_active 2 change_required_masked_active

run_check static 3 static
grep -Fq 'ERROR: unexpected earlyoom state; refusing to change it' \
  "$fixture_dir/static.out"
run_check invalid_enabled_rc 3 unexpected
run_check invalid_active_rc 3 unexpected

run_apply_refusal() {
  local initial_state="$1"
  local state_file="$fixture_dir/$initial_state.state"
  local action_log="$fixture_dir/$initial_state.actions"
  local output_file="$fixture_dir/$initial_state.apply.out"
  local rc

  printf '%s\n' "$initial_state" > "$state_file"
  : > "$action_log"
  set +e
  PATH="$fixture_dir:$PATH" \
    EARLYOOM_TEST_STATE="$initial_state" \
    EARLYOOM_TEST_STATE_FILE="$state_file" \
    EARLYOOM_TEST_ACTION_LOG="$action_log" \
    bash "$script" --apply > "$output_file" 2>&1
  rc=$?
  set -e
  test "$rc" -eq 3
  grep -Fq 'ERROR: refusing to mutate a platform-managed earlyoom service' \
    "$output_file"
  test ! -s "$action_log"
  test "$(cat "$state_file")" = "$initial_state"
}

run_apply_refusal enabled_active
run_apply_refusal disabled_active
run_apply_refusal masked_active

printf 'earlyoom safeguard: PASS\n'

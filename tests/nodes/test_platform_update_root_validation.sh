#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
validator="$repo_root/nodes/bin/validate-platform-update-root"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT

mkdir -p "$test_dir/bin"

cat > "$test_dir/bin/docker" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$DOCKER_ARGS_FILE"
printf 'GPU visible\n'
FAKE

cat > "$test_dir/bin/journalctl" <<'FAKE'
#!/usr/bin/env bash
printf '%s' "${JOURNAL_CONTENT:-}"
printf '%s' "${JOURNAL_DIAGNOSTIC:-}" >&2
exit "${JOURNAL_EXIT:-0}"
FAKE

cat > "$test_dir/bin/cat" <<'FAKE'
#!/usr/bin/env bash
if [[ "$1" == '/proc/sys/kernel/random/boot_id' ]]; then
  printf '11111111-2222-3333-4444-555555555555\n'
else
  exec /bin/cat "$@"
fi
FAKE

chmod +x "$test_dir/bin/docker" "$test_dir/bin/journalctl" "$test_dir/bin/cat"

export PATH="$test_dir/bin:$PATH"
export DOCKER_ARGS_FILE="$test_dir/docker-args"

current_boot_id='11111111222233334444555555555555'
JOURNAL_CONTENT="{\"_BOOT_ID\":\"$current_boot_id\",\"MESSAGE\":\"normal kernel message\"}" \
  "$validator" > "$test_dir/safe-output"

cat > "$test_dir/expected-docker-args" <<'EXPECTED'
run
--rm
--pull=never
--gpus=all
nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04@sha256:7d2f6a8c2071d911524f95061a0db363e24d27aa51ec831fcccf9e76eb72bc92
nvidia-smi
EXPECTED

cmp "$test_dir/expected-docker-args" "$DOCKER_ARGS_FILE"
grep -Fxq 'PASS: GPU container and current-boot storage checks passed' \
  "$test_dir/safe-output"

JOURNAL_CONTENT="{\"_BOOT_ID\":\"$current_boot_id\",\"MESSAGE\":\"nvme nvme0: Shutdown timeout set to 10 seconds\"}" \
  "$validator" > "$test_dir/benign-timeout-output"
grep -Fxq 'PASS: GPU container and current-boot storage checks passed' \
  "$test_dir/benign-timeout-output"

if JOURNAL_CONTENT='' JOURNAL_DIAGNOSTIC='No journal files were found.' \
  "$validator" > "$test_dir/no-journal-output" 2>&1
then
  printf 'validator accepted an empty current-boot journal\n' >&2
  exit 1
fi
grep -Fq 'FAIL: no current-boot kernel journal entries' \
  "$test_dir/no-journal-output"

if JOURNAL_CONTENT='{"_BOOT_ID":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","MESSAGE":"normal old-boot message"}' \
  "$validator" > "$test_dir/wrong-boot-output" 2>&1
then
  printf 'validator accepted a journal from another boot\n' >&2
  exit 1
fi
grep -Fq 'FAIL: kernel journal does not match the running boot' \
  "$test_dir/wrong-boot-output"

dangerous_messages=(
  'nvme nvme0: I/O 42 QID 7 timeout, aborting'
  'nvme nvme0: controller is down; will reset: CSTS=0xffffffff'
  'nvme0n1: I/O error at sector 123'
)

for message in "${dangerous_messages[@]}"; do
  if JOURNAL_CONTENT="{\"_BOOT_ID\":\"$current_boot_id\",\"MESSAGE\":\"$message\"}" \
    "$validator" \
    > "$test_dir/unsafe-output" 2>&1
  then
    printf 'validator accepted a kernel storage error: %s\n' "$message" >&2
    exit 1
  fi

  grep -Fq "$message" "$test_dir/unsafe-output"
  grep -Fq 'FAIL: storage or filesystem error in current boot' \
    "$test_dir/unsafe-output"
done

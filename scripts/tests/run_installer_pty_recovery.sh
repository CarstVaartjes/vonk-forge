#!/usr/bin/env bash
set -euo pipefail

test_binary="${1:?provide the compiled process_boundary test binary}"
test -x "$test_binary"
test "$(uname -m)" = aarch64

docker run --rm --platform linux/arm64 \
  --mount "type=bind,src=$test_binary,dst=/test/process_boundary,readonly" \
  ubuntu:24.04 /bin/bash -euc '
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      openssl sudo passwd util-linux >/dev/null
    id ubuntu >/dev/null 2>&1 || /usr/sbin/useradd -m -s /bin/bash ubuntu
    printf "ubuntu:test-password\n" | /usr/sbin/chpasswd
    /usr/sbin/usermod -aG sudo ubuntu
    /test/process_boundary --exact \
      real_sudo_pty_foreground_auth_then_recover_without_reinstall \
      --ignored --nocapture
  '

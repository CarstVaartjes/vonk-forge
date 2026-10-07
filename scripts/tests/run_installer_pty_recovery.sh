#!/usr/bin/env bash
set -euo pipefail

test_binary="${1:?provide the compiled process_boundary test binary}"
test -x "$test_binary"
test "$(uname -m)" = aarch64

script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

docker run --rm --platform linux/arm64 \
  --mount "type=bind,src=/etc/ssl/certs/ca-certificates.crt,dst=/etc/ssl/certs/ca-certificates.crt,readonly" \
  --mount "type=bind,src=$script_root/ci-apt-install,dst=/test/ci-apt-install,readonly" \
  --mount "type=bind,src=$test_binary,dst=/test/process_boundary,readonly" \
  ubuntu:24.04 /bin/bash -euc '
    /test/ci-apt-install openssl sudo passwd util-linux
    id ubuntu >/dev/null 2>&1 || /usr/sbin/useradd -m -s /bin/bash ubuntu
    printf "ubuntu:test-password\n" | /usr/sbin/chpasswd
    /usr/sbin/usermod -aG sudo ubuntu
    /test/process_boundary --exact \
      real_sudo_pty_foreground_auth_then_recover_without_reinstall \
      --ignored --nocapture
  '

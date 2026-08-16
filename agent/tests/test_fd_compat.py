from __future__ import annotations

import fcntl
import os
import signal


def test_memfd_compat_restores_linux_bindings_when_python_omits_them(
    monkeypatch,
) -> None:
    from vonk_agent import fd_compat

    monkeypatch.delattr(os, "memfd_create", raising=False)
    monkeypatch.delattr(os, "MFD_CLOEXEC", raising=False)
    monkeypatch.delattr(os, "MFD_ALLOW_SEALING", raising=False)
    for name in (
        "F_ADD_SEALS",
        "F_GET_SEALS",
        "F_SEAL_SEAL",
        "F_SEAL_SHRINK",
        "F_SEAL_GROW",
        "F_SEAL_WRITE",
        "F_SEAL_FUTURE_WRITE",
    ):
        monkeypatch.delattr(fcntl, name, raising=False)
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)

    fd_compat.ensure_memfd_support()
    descriptor = os.memfd_create(
        "vonk-test", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
    )
    try:
        os.write(descriptor, b"sealed")
        fcntl.fcntl(
            descriptor,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE,
        )
        os.lseek(descriptor, 0, os.SEEK_SET)
        assert os.read(descriptor, 64) == b"sealed"
    finally:
        os.close(descriptor)

    child = os.fork()
    if child == 0:
        os.pause()
    try:
        pidfd = os.pidfd_open(child)
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        finally:
            os.close(pidfd)
        _, status = os.waitpid(child, 0)
        assert os.WIFSIGNALED(status)
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass

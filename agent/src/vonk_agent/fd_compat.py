"""Small Linux file-descriptor compatibility helpers."""

from __future__ import annotations

import ctypes
import fcntl
import os
import signal
import sys

_MFD_CLOEXEC = 0x0001
_MFD_ALLOW_SEALING = 0x0002
_F_ADD_SEALS = 1033
_F_GET_SEALS = 1034
_F_SEAL_SEAL = 0x0001
_F_SEAL_SHRINK = 0x0002
_F_SEAL_GROW = 0x0004
_F_SEAL_WRITE = 0x0008
_F_SEAL_FUTURE_WRITE = 0x0010
_SYS_PIDFD_SEND_SIGNAL = 424
_SYS_PIDFD_OPEN = 434


def _libc_memfd_create(name: str, flags: int) -> int:
    if sys.platform != "linux":
        raise OSError("memfd_create is only supported on Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.memfd_create
    function.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    descriptor = function(os.fsencode(name), flags)
    if descriptor < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return descriptor


def _libc_pidfd_open(pid: int, flags: int = 0) -> int:
    if sys.platform != "linux":
        raise OSError("pidfd_open is only supported on Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    descriptor = syscall(_SYS_PIDFD_OPEN, pid, flags)
    if descriptor < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(descriptor)


def _libc_pidfd_send_signal(
    pidfd: int,
    signal_number: int,
    siginfo: object | None = None,
    flags: int = 0,
) -> None:
    if sys.platform != "linux":
        raise OSError("pidfd_send_signal is only supported on Linux")
    if siginfo is not None:
        raise NotImplementedError("siginfo is not supported by the compatibility shim")
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    result = syscall(
        _SYS_PIDFD_SEND_SIGNAL,
        pidfd,
        signal_number,
        ctypes.c_void_p(),
        flags,
    )
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def ensure_memfd_support() -> None:
    """Provide Linux descriptor bindings omitted by some Python distributions."""

    if not hasattr(os, "MFD_CLOEXEC"):
        os.MFD_CLOEXEC = _MFD_CLOEXEC
    if not hasattr(os, "MFD_ALLOW_SEALING"):
        os.MFD_ALLOW_SEALING = _MFD_ALLOW_SEALING
    if not hasattr(os, "memfd_create"):
        os.memfd_create = _libc_memfd_create
    if not hasattr(os, "pidfd_open"):
        os.pidfd_open = _libc_pidfd_open
    if not hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal = _libc_pidfd_send_signal
    for name, value in (
        ("F_ADD_SEALS", _F_ADD_SEALS),
        ("F_GET_SEALS", _F_GET_SEALS),
        ("F_SEAL_SEAL", _F_SEAL_SEAL),
        ("F_SEAL_SHRINK", _F_SEAL_SHRINK),
        ("F_SEAL_GROW", _F_SEAL_GROW),
        ("F_SEAL_WRITE", _F_SEAL_WRITE),
        ("F_SEAL_FUTURE_WRITE", _F_SEAL_FUTURE_WRITE),
    ):
        if not hasattr(fcntl, name):
            setattr(fcntl, name, value)

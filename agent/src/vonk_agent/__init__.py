"""Outbound Vonk Forge agent runtime primitives."""

from .fd_compat import ensure_memfd_support

ensure_memfd_support()

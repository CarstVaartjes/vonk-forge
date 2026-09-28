"""Canonical platform harness metadata and projection contracts."""

from ..runtime_writable_paths import EngineTelemetryContract, RuntimeWritablePath
from .canonical_metadata import CANONICAL_HARNESSES, CanonicalHarnessMetadata
from .common import HarnessCompileError
from .contracts import HarnessBinding, HarnessMount, HarnessProjection

__all__ = [
    "CANONICAL_HARNESSES",
    "CanonicalHarnessMetadata",
    "EngineTelemetryContract",
    "HarnessBinding",
    "HarnessCompileError",
    "HarnessMount",
    "HarnessProjection",
    "RuntimeWritablePath",
]

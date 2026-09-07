"""Schema-v1 execution-harness registry exports."""

from ..runtime_writable_paths import EngineTelemetryContract, RuntimeWritablePath
from .canonical_metadata import CANONICAL_HARNESSES, CanonicalHarnessMetadata
from .common import HarnessCompileError
from .contracts import HarnessBinding, HarnessCompiler, HarnessMount, HarnessProjection
from .registry import BUILTIN_HARNESS_SLUGS, HarnessRegistry, TrustedBuiltinComposition

__all__ = [
    "BUILTIN_HARNESS_SLUGS",
    "CANONICAL_HARNESSES",
    "CanonicalHarnessMetadata",
    "EngineTelemetryContract",
    "HarnessBinding",
    "HarnessCompileError",
    "HarnessCompiler",
    "HarnessMount",
    "HarnessProjection",
    "HarnessRegistry",
    "RuntimeWritablePath",
    "TrustedBuiltinComposition",
]

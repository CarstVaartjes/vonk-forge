"""Policy for runtime-distribution-declared environment variables."""

from __future__ import annotations

import re
from collections.abc import Mapping

_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_FORBIDDEN_NAMES = frozenset(
    {
        "BASH_ENV",
        "CUDA_INJECTION32_PATH",
        "CUDA_INJECTION64_PATH",
        "ENV",
        "GCONV_PATH",
        "NODE_OPTIONS",
        "PATH",
        "PERL5OPT",
        "PYTHONBREAKPOINT",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "RUBYOPT",
        "SHELLOPTS",
    }
)
_FORBIDDEN_PREFIXES = ("DYLD_", "LD_", "VONK_")


def distribution_allowed_environment(capabilities: object) -> frozenset[str]:
    """Return the bounded environment authority in a distribution capability."""

    if capabilities is None:
        return frozenset()
    if not isinstance(capabilities, Mapping):
        raise TypeError("runtime distribution capabilities are invalid")
    capability = capabilities.get("runtime_environment")
    if capability is None:
        return frozenset()
    if not isinstance(capability, Mapping):
        raise TypeError("runtime environment capability is invalid")
    if set(capability) != {"allowed_names"}:
        raise ValueError("runtime environment capability is invalid")
    allowed_names = capability.get("allowed_names")
    if type(allowed_names) is not list:
        raise TypeError("runtime environment allowed names are invalid")
    if not 1 <= len(allowed_names) <= 128:
        raise ValueError("runtime environment allowed names are invalid")
    seen: set[str] = set()
    for name in allowed_names:
        if type(name) is not str:
            raise TypeError("runtime environment allowed name is invalid")
        if (
            _ENVIRONMENT_NAME.fullmatch(name) is None
            or name in _FORBIDDEN_NAMES
            or name.startswith(_FORBIDDEN_PREFIXES)
        ):
            raise ValueError("runtime environment allowed name is unsafe")
        if name in seen:
            raise ValueError("runtime environment allowed names are invalid")
        seen.add(name)
    return frozenset(allowed_names)

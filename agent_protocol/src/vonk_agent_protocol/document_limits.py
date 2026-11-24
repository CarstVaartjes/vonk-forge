"""Byte ceilings of protocol documents and of the host-runtime request that carries them."""

from __future__ import annotations

# Backstop above a full 4096-object distribution assignment (~0.5 MiB); the
# real GLM shape is ~18 KiB, only 3x under the previous 64 KiB.
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES = (
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES + MAX_DOCUMENT_BYTES
)
# A complete runtime request carries its typed plan plus one frame's worth of
# projected argv and envelope. The signed helper frame itself carries only the
# grant and request digest, never the request document.
MAX_HELPER_FRAME_BYTES = 1024 * 1024
MAX_HOST_RUNTIME_REQUEST_BYTES = (
    MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES + MAX_HELPER_FRAME_BYTES
)
HOST_RUNTIME_REQUEST_ENVELOPE_BYTES = 16 * 1024
MAX_HOST_RUNTIME_ARGUMENT_BYTES = (
    MAX_HELPER_FRAME_BYTES - HOST_RUNTIME_REQUEST_ENVELOPE_BYTES
)
MAX_ARGV_BYTES = MAX_HOST_RUNTIME_ARGUMENT_BYTES

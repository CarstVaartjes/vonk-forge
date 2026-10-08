"""Plain build receipt snapshots and managed-storage lookup contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CompletedRecipeBuild:
    build_id: str
    image_digest: str
    oci_layout_sha256: str
    image_bytes: int


@dataclass(frozen=True, slots=True)
class BuildCandidate:
    """Plain snapshot of one succeeded build row, safe to use after commit.

    Managed storage is consulted only after the reading transaction has ended,
    so a candidate carries the values that decision needs instead of keeping an
    ORM instance alive across storage I/O.
    """

    build_id: str
    builder_node_id: str
    build_input_sha256: str
    builder_binary_digest: str
    image_digest: str
    oci_layout_sha256: str
    image_bytes: int


class PreparedBuildReceipt(Protocol):
    """Verified filesystem identity of one prepared Controller build."""

    build_id: str
    build_input_sha256: str | None
    image_digest: str
    oci_archive_sha256: str
    image_bytes: int


class PreparedBuildLookup(Protocol):
    """Answer whether exact build bytes are already prepared on disk.

    The lookup is keyed by the executable build input identity the receipt
    recorded beside the archive. It is the reuse owner, so it never consults the
    SQL build index and never falls back to a weaker key.
    """

    def __call__(
        self,
        build_input_sha256: str,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        expected_archive_sha256: str | None = None,
    ) -> PreparedBuildReceipt | None: ...

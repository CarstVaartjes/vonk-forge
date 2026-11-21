"""The Controller's own build identity.

``tools/stamp_build_identity.py`` writes ``build-identity.json`` into this
package at image build time; this module only reads it. The same writer stamps
the CLI wheel, so one source tree gives the Controller and vonkctl the same
``control_contract_sha256``. scripts/generate-control-clients ships a copy of
``control_limits`` to the CLI; the CLI keeps its own stdlib
``contract_fingerprint`` (control/tests/test_cli_update_contract.py keeps the two equal).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib.resources import files


def contract_fingerprint(document: object) -> str:
    return hashlib.sha256(
        json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class RuntimeBuildIdentity:
    source_sha: str | None
    control_contract_sha256: str | None
    worker_contract_sha256: str | None
    #: The Git tree the build came from. A release that reuses a pull request
    #: build keeps that build's commit, but the tree equals the release's.
    source_tree: str | None = None


def _hex(value: object, length: int) -> str | None:
    if isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{length}}}", value):
        return value
    return None


def packaged_runtime_identity() -> RuntimeBuildIdentity:
    """The identity the build stamped, as written; unstamped or damaged is unknown."""
    unknown = RuntimeBuildIdentity(None, None, None)
    try:
        value = json.loads(
            files("vonk_control").joinpath("build-identity.json").read_text()
        )
    except (OSError, ValueError):
        return unknown
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        return unknown
    stamped = RuntimeBuildIdentity(
        _hex(value.get("source_sha"), 40),
        _hex(value.get("control_contract_sha256"), 64),
        _hex(value.get("worker_contract_sha256"), 64),
        _hex(value.get("source_tree"), 40),
    )
    if (
        stamped.control_contract_sha256 is None
        or stamped.worker_contract_sha256 is None
    ):
        return unknown
    return stamped

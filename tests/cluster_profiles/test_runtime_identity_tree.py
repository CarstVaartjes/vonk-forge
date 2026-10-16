"""A build's Git tree is reported beside its commit, informatively."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cluster_profiles import runtime_identity
from cluster_profiles.runtime_identity import contract_fingerprint


@pytest.mark.parametrize(
    ("stamped", "expected"),
    [("1" * 40, "1" * 40), (None, None), ("not-a-tree", None)],
)
def test_the_packaged_tree_is_reported_and_never_decides_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stamped: str | None,
    expected: str | None,
) -> None:
    """Catches a build whose tree is lost on the way to the platform view, and
    a missing or damaged tree that would make a valid build unknown."""
    schema = {"openapi": "3.1.0"}
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas/control-openapi.json").write_text(json.dumps(schema))
    identity = {
        "schema_version": 2,
        "source_sha": "a" * 40,
        "control_contract_sha256": contract_fingerprint(schema),
        "worker_contract_sha256": "d" * 64,
    }
    if stamped is not None:
        identity["source_tree"] = stamped
    (tmp_path / "build-identity.json").write_text(json.dumps(identity))
    monkeypatch.setattr(runtime_identity, "files", lambda _package: tmp_path)

    observed = runtime_identity.packaged_runtime_identity()

    assert observed.source_tree == expected
    assert observed.source_sha == "a" * 40
    assert observed.worker_contract_sha256 == "d" * 64

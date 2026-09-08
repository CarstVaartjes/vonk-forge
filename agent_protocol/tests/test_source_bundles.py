from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.source_bundles import (
    SourceBundleDigestManifest,
    SourceBundleManifest,
)


def manifest_document():
    return {
        "schema_version": 1,
        "files": [{"path": "empty", "mode": 420, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}],
        "total_bytes": 0,
    }


def test_source_manifest_json_roundtrip_preserves_required_zero_and_digest_bytes():
    raw = manifest_document()
    manifest = SourceBundleDigestManifest.model_validate_json(json.dumps(raw))
    encoded = canonical_message(manifest)
    assert json.loads(encoded) == raw
    assert manifest.digest() == hashlib.sha256(encoded).hexdigest()
    metadata = SourceBundleManifest.model_validate_json(json.dumps(raw | {"sha256": manifest.digest()}))
    assert metadata.digest() == manifest.digest()
    assert SourceBundleManifest.model_validate_json(canonical_message(metadata)) == metadata


@pytest.mark.parametrize("field", ["schema_version", "files", "total_bytes"])
@pytest.mark.parametrize("null", [False, True])
def test_source_manifest_rejects_missing_or_null_required_fields(field, null):
    raw = manifest_document()
    if null:
        raw[field] = None
    else:
        raw.pop(field)
    with pytest.raises(ValidationError):
        SourceBundleDigestManifest.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("change", [
    {"mode": 0o777}, {"size": True}, {"path": "../escape"},
    {"sha256": "A" * 64}, {"path": "é" * 257}, {"extra": 1},
])
def test_source_manifest_rejects_malformed_file_identity(change):
    raw = manifest_document()
    raw["files"][0].update(change)
    with pytest.raises(ValidationError):
        SourceBundleDigestManifest.model_validate_json(json.dumps(raw))


def test_source_manifest_rejects_inconsistent_totals_duplicates_and_stored_digest():
    raw = manifest_document()
    for invalid in (raw | {"total_bytes": 1}, raw | {"files": raw["files"] * 2}):
        with pytest.raises(ValidationError):
            SourceBundleDigestManifest.model_validate_json(json.dumps(invalid))
    with pytest.raises(ValidationError, match="digest does not match"):
        SourceBundleManifest.model_validate_json(json.dumps(raw | {"sha256": "f" * 64}))

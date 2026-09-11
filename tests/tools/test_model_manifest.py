from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import NotRequired, Self, TypedDict

import pytest

from tools.model_manifest import ManifestError, generate, validate_manifest, verify

REPOSITORY = "deepseek-ai/DeepSeek-V4-Flash-0731"
REVISION = "9e165c30e2704aec5d9d593cce3eebd58bbef1cb"


class FakeResponse(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class FakeOpener:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def __call__(self, url: str, *, timeout: int) -> FakeResponse:
        self.requested.append(url)
        return FakeResponse(self.responses[url])


def _api_url() -> str:
    return (
        "https://huggingface.co/api/models/deepseek-ai/"
        f"DeepSeek-V4-Flash-0731/revision/{REVISION}?blobs=true"
    )


def _resolve_url(path: str) -> str:
    return (
        f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{path}"
        "?download=true"
    )


class _Sibling(TypedDict):
    rfilename: str
    blobId: str
    size: int
    lfs: NotRequired[dict[str, object]]


class _ModelMetadata(TypedDict):
    sha: str
    siblings: list[_Sibling]


class _ManifestFile(TypedDict):
    path: str
    size: int
    sha256: str
    blob_id: str


class _Manifest(TypedDict):
    schema_version: int
    repository: str
    revision: str
    encoder_path: str
    weight_index_path: str
    file_count: int
    weight_shard_count: int
    total_bytes: int
    safetensors_bytes: int
    files: list[_ManifestFile]


def _repository_responses() -> tuple[_ModelMetadata, dict[str, bytes]]:
    shards = [f"model-{number:05d}-of-00048.safetensors" for number in range(1, 49)]
    index_content = json.dumps(
        {
            "metadata": {"total_size": 1_656},
            "weight_map": {
                f"model.layers.{number}.weight": shard
                for number, shard in enumerate(shards, start=1)
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    non_lfs = {
        "encoding/encoding_dsv4.py": b"encoder\n",
        "model.safetensors.index.json": index_content,
        **{f"extra-{number:02d}.txt": b"x" for number in range(24)},
    }
    siblings: list[_Sibling] = [
        {
            "rfilename": shard,
            "blobId": f"{number:040x}",
            "size": 10 + number,
            "lfs": {
                "sha256": f"{number:064x}",
                "size": 10 + number,
                "pointerSize": 133,
            },
        }
        for number, shard in enumerate(shards, start=1)
    ]
    siblings.extend(
        {
            "rfilename": path,
            "blobId": hashlib.sha1(content).hexdigest(),
            "size": len(content),
        }
        for path, content in non_lfs.items()
    )
    metadata: _ModelMetadata = {"sha": REVISION, "siblings": siblings}
    responses = {
        _api_url(): json.dumps(metadata, separators=(",", ":")).encode(),
        **{_resolve_url(path): content for path, content in non_lfs.items()},
    }
    return metadata, responses


def _snapshot_manifest() -> tuple[_Manifest, dict[str, bytes]]:
    files: dict[str, bytes] = {
        **{
            f"model-{number:05d}-of-00048.safetensors": f"shard-{number:02d}".encode()
            for number in range(1, 49)
        },
        "encoding/encoding_dsv4.py": b"encoder\n",
        "model.safetensors.index.json": b"{}",
        **{f"extra-{number:02d}.txt": b"x" for number in range(24)},
    }
    entries: list[_ManifestFile] = [
        {
            "path": path,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "blob_id": hashlib.sha1(content).hexdigest(),
        }
        for path, content in sorted(files.items())
    ]
    manifest: _Manifest = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "revision": REVISION,
        "encoder_path": "encoding/encoding_dsv4.py",
        "weight_index_path": "model.safetensors.index.json",
        "file_count": 74,
        "weight_shard_count": 48,
        "total_bytes": sum(len(content) for content in files.values()),
        "safetensors_bytes": sum(
            len(content)
            for path, content in files.items()
            if path.endswith(".safetensors")
        ),
        "files": entries,
    }
    return manifest, files


def _materialize(snapshot_dir: Path, files: dict[str, bytes]) -> None:
    for path, content in files.items():
        destination = snapshot_dir / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def test_generation_rejects_revision_mismatch() -> None:
    api_url = _api_url()
    opener = FakeOpener(
        {
            api_url: json.dumps(
                {"sha": "0" * 40, "siblings": []}, separators=(",", ":")
            ).encode()
        }
    )

    with pytest.raises(ManifestError, match="revision mismatch"):
        generate(
            REPOSITORY, REVISION, opener=opener
        )

    assert opener.requested == [api_url]


def test_generation_uses_lfs_hashes_and_fetches_only_non_lfs_files() -> None:
    _, responses = _repository_responses()
    opener = FakeOpener(responses)

    manifest = generate(REPOSITORY, REVISION, opener=opener)

    entries = {entry["path"]: entry for entry in manifest["files"]}
    first_shard = entries["model-00001-of-00048.safetensors"]
    assert first_shard == {
        "path": "model-00001-of-00048.safetensors",
        "size": 11,
        "sha256": f"{1:064x}",
        "blob_id": f"{1:040x}",
    }
    assert entries["encoding/encoding_dsv4.py"]["sha256"] == hashlib.sha256(
        b"encoder\n"
    ).hexdigest()
    assert manifest["file_count"] == 74
    assert manifest["weight_shard_count"] == 48
    assert manifest["safetensors_bytes"] == 1_656
    assert len(opener.requested) == 27
    assert all(".safetensors?" not in url for url in opener.requested)


def test_generation_requires_all_48_index_shards() -> None:
    metadata, responses = _repository_responses()
    index_url = _resolve_url("model.safetensors.index.json")
    index = json.loads(responses[index_url])
    index["weight_map"].pop("model.layers.48.weight")
    responses[index_url] = json.dumps(index, separators=(",", ":")).encode()
    index_sibling = next(
        sibling
        for sibling in metadata["siblings"]
        if sibling["rfilename"] == "model.safetensors.index.json"
    )
    index_sibling["size"] = len(responses[index_url])
    responses[_api_url()] = json.dumps(metadata, separators=(",", ":")).encode()

    with pytest.raises(ManifestError, match="48 referenced shards"):
        generate(REPOSITORY, REVISION, opener=FakeOpener(responses))


def test_manifest_rejects_unsafe_paths() -> None:
    manifest, _ = _snapshot_manifest()
    manifest["files"][1]["path"] = "../outside"

    with pytest.raises(ManifestError, match="unsafe manifest path"):
        validate_manifest(manifest)


def test_verification_accepts_a_complete_materialized_snapshot(tmp_path: Path) -> None:
    manifest, files = _snapshot_manifest()
    _materialize(tmp_path, files)

    report = verify(manifest, tmp_path)

    assert report.ok is True
    assert report.missing == ()
    assert report.changed == ()
    assert report.unsafe == ()
    assert report.verified_files == 74
    assert report.verified_bytes == manifest["total_bytes"]


def test_verification_reports_missing_and_changed_shards(tmp_path: Path) -> None:
    manifest, files = _snapshot_manifest()
    _materialize(tmp_path, files)
    (tmp_path / "model-00001-of-00048.safetensors").unlink()
    (tmp_path / "model-00002-of-00048.safetensors").write_bytes(b"SHARD-02")

    report = verify(manifest, tmp_path)

    assert report.ok is False
    assert report.missing == ("model-00001-of-00048.safetensors",)
    assert report.changed == ("model-00002-of-00048.safetensors",)
    assert report.unsafe == ()


def test_verification_rejects_symlinks_and_non_regular_files(tmp_path: Path) -> None:
    manifest, files = _snapshot_manifest()
    _materialize(tmp_path, files)
    encoder = tmp_path / "encoding/encoding_dsv4.py"
    encoder.unlink()
    encoder.symlink_to(tmp_path / "extra-00.txt")
    non_regular = tmp_path / "extra-01.txt"
    non_regular.unlink()
    non_regular.mkdir()

    report = verify(manifest, tmp_path)

    assert report.ok is False
    assert report.unsafe == (
        "encoding/encoding_dsv4.py",
        "extra-01.txt",
    )


def test_verification_rejects_unmanifested_entries_and_unsafe_symlinks(
    tmp_path: Path,
) -> None:
    manifest, files = _snapshot_manifest()
    _materialize(tmp_path, files)
    (tmp_path / "rogue.txt").write_text("unexpected", encoding="utf-8")
    (tmp_path / "rogue-directory").mkdir()
    (tmp_path / "rogue-directory/rogue-link").symlink_to(
        tmp_path / "extra-00.txt"
    )
    (tmp_path / "encoding/rogue-link").symlink_to(tmp_path / "extra-00.txt")

    report = verify(manifest, tmp_path)

    assert report.ok is False
    assert report.unexpected == ("rogue-directory", "rogue.txt")
    assert report.unsafe == (
        "encoding/rogue-link",
        "rogue-directory/rogue-link",
    )


def test_manifest_requires_pinned_encoder() -> None:
    manifest = {
        "schema_version": 1,
        "repository": "deepseek-ai/DeepSeek-V4-Flash-0731",
        "revision": "9e165c30e2704aec5d9d593cce3eebd58bbef1cb",
        "encoder_path": "encoding/encoding_dsv4.py",
        "weight_index_path": "model.safetensors.index.json",
        "file_count": 1,
        "weight_shard_count": 0,
        "total_bytes": 2,
        "safetensors_bytes": 0,
        "files": [
            {
                "path": "model.safetensors.index.json",
                "size": 2,
                "sha256": "44" * 32,
                "blob_id": "55" * 20,
            }
        ],
    }

    with pytest.raises(ManifestError, match="encoding/encoding_dsv4.py"):
        validate_manifest(manifest)

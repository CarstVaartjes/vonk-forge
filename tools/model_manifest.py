"""Build and verify immutable Hugging Face model snapshot manifests."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen

ENCODER_PATH = "encoding/encoding_dsv4.py"
WEIGHT_INDEX_PATH = "model.safetensors.index.json"
EXPECTED_FILE_COUNT = 74
EXPECTED_SHARD_COUNT = 48
HASH_CHUNK_BYTES = 8 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ManifestError(ValueError):
    """Raised when repository metadata or a model manifest is invalid."""


@dataclass(frozen=True)
class VerificationReport:
    """Machine-readable result of a completely offline snapshot check."""

    missing: tuple[str, ...]
    changed: tuple[str, ...]
    unsafe: tuple[str, ...]
    unexpected: tuple[str, ...]
    verified_files: int
    verified_bytes: int

    @property
    def ok(self) -> bool:
        return not (self.missing or self.changed or self.unsafe or self.unexpected)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, **asdict(self)}


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise ManifestError(f"unsafe manifest path: {value!r}")
    parts = value.split("/")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ManifestError(f"unsafe manifest path: {value!r}")
    return value


def _require_int(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ManifestError(f"manifest {key} must be a non-negative integer")
    return value


def _expected_shards() -> set[str]:
    return {
        f"model-{number:05d}-of-{EXPECTED_SHARD_COUNT:05d}.safetensors"
        for number in range(1, EXPECTED_SHARD_COUNT + 1)
    }


def generate(
    repo_id: str,
    revision: str,
    *,
    opener: Any = urlopen,
) -> dict[str, Any]:
    """Build a manifest from metadata for one immutable repository revision."""

    encoded_repo = quote(repo_id, safe="/")
    encoded_revision = quote(revision, safe="")
    api_url = (
        f"https://huggingface.co/api/models/{encoded_repo}/revision/"
        f"{encoded_revision}?blobs=true"
    )
    with opener(api_url, timeout=30) as response:
        metadata = json.load(response)
    if not isinstance(metadata, Mapping):
        raise ManifestError("revision API did not return an object")
    if metadata.get("sha") != revision:
        raise ManifestError(
            f"revision mismatch: requested {revision}, received {metadata.get('sha')!r}"
        )
    siblings = metadata.get("siblings")
    if not isinstance(siblings, list) or len(siblings) != EXPECTED_FILE_COUNT:
        actual = len(siblings) if isinstance(siblings, list) else "invalid"
        raise ManifestError(
            f"expected {EXPECTED_FILE_COUNT} revision files, received {actual}"
        )

    entries: list[dict[str, Any]] = []
    non_lfs_content: dict[str, bytes] = {}
    seen: set[str] = set()
    for sibling in sorted(siblings, key=lambda item: item.get("rfilename", "")):
        if not isinstance(sibling, Mapping):
            raise ManifestError("revision API sibling must be an object")
        path = _safe_relative_path(sibling.get("rfilename"))
        if path in seen:
            raise ManifestError(f"duplicate revision file: {path}")
        seen.add(path)
        blob_id = sibling.get("blobId")
        if not isinstance(blob_id, str) or not _GIT_SHA_RE.fullmatch(blob_id):
            raise ManifestError(f"invalid blobId provenance for {path}")

        lfs = sibling.get("lfs")
        if lfs is not None:
            if not isinstance(lfs, Mapping):
                raise ManifestError(f"invalid LFS metadata for {path}")
            sha256 = lfs.get("sha256")
            size = lfs.get("size")
            if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
                raise ManifestError(f"invalid LFS SHA-256 for {path}")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ManifestError(f"invalid LFS size for {path}")
            if sibling.get("size") != size:
                raise ManifestError(f"LFS size mismatch for {path}")
        else:
            if path.endswith(".safetensors"):
                raise ManifestError(f"weight lacks LFS metadata: {path}")
            encoded_path = quote(path, safe="/")
            raw_url = (
                f"https://huggingface.co/{encoded_repo}/resolve/{encoded_revision}/"
                f"{encoded_path}?download=true"
            )
            with opener(raw_url, timeout=30) as response:
                content = response.read()
            size = len(content)
            if sibling.get("size") != size:
                raise ManifestError(f"pinned file size mismatch for {path}")
            sha256 = hashlib.sha256(content).hexdigest()
            if path == WEIGHT_INDEX_PATH:
                non_lfs_content[path] = content

        entries.append(
            {
                "path": path,
                "size": size,
                "sha256": sha256,
                "blob_id": blob_id,
            }
        )

    try:
        weight_index = json.loads(non_lfs_content[WEIGHT_INDEX_PATH])
        weight_map = weight_index["weight_map"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ManifestError("invalid pinned weight index") from error
    if not isinstance(weight_map, Mapping):
        raise ManifestError("invalid pinned weight index weight_map")
    referenced_shards = set(weight_map.values())
    if referenced_shards != _expected_shards():
        raise ManifestError(
            f"weight index must contain all {EXPECTED_SHARD_COUNT} referenced shards"
        )
    metadata_shards = {
        entry["path"] for entry in entries if entry["path"].endswith(".safetensors")
    }
    if metadata_shards != referenced_shards:
        raise ManifestError("weight index and revision shard metadata differ")

    manifest = {
        "schema_version": 1,
        "repository": repo_id,
        "revision": revision,
        "encoder_path": ENCODER_PATH,
        "weight_index_path": WEIGHT_INDEX_PATH,
        "file_count": len(entries),
        "weight_shard_count": len(metadata_shards),
        "total_bytes": sum(entry["size"] for entry in entries),
        "safetensors_bytes": sum(
            entry["size"] for entry in entries if entry["path"].endswith(".safetensors")
        ),
        "files": entries,
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    """Validate the complete DeepSeek snapshot manifest contract."""

    files = manifest.get("files")
    paths = {entry.get("path") for entry in files or () if isinstance(entry, Mapping)}
    if manifest.get("encoder_path") != ENCODER_PATH or ENCODER_PATH not in paths:
        raise ManifestError(f"manifest must include {ENCODER_PATH}")
    if (
        manifest.get("weight_index_path") != WEIGHT_INDEX_PATH
        or WEIGHT_INDEX_PATH not in paths
    ):
        raise ManifestError(f"manifest must include {WEIGHT_INDEX_PATH}")
    if manifest.get("schema_version") != 1:
        raise ManifestError("unsupported manifest schema_version")
    if not isinstance(manifest.get("repository"), str) or not manifest["repository"]:
        raise ManifestError("manifest repository must be non-empty")
    revision = manifest.get("revision")
    if not isinstance(revision, str) or not _GIT_SHA_RE.fullmatch(revision):
        raise ManifestError("manifest revision must be a full Git SHA")
    if not isinstance(files, list):
        raise ManifestError("manifest files must be a list")

    entries: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, Mapping):
            raise ManifestError("manifest file entry must be an object")
        path = _safe_relative_path(entry.get("path"))
        if path in seen:
            raise ManifestError(f"duplicate manifest path: {path}")
        seen.add(path)
        _require_int(entry, "size")
        sha256 = entry.get("sha256")
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise ManifestError(f"invalid SHA-256 for {path}")
        blob_id = entry.get("blob_id")
        if not isinstance(blob_id, str) or not _GIT_SHA_RE.fullmatch(blob_id):
            raise ManifestError(f"invalid blob_id provenance for {path}")
        entries.append(entry)

    file_count = _require_int(manifest, "file_count")
    shard_count = _require_int(manifest, "weight_shard_count")
    total_bytes = _require_int(manifest, "total_bytes")
    safetensors_bytes = _require_int(manifest, "safetensors_bytes")
    actual_shards = {
        entry["path"] for entry in entries if entry["path"].endswith(".safetensors")
    }
    if file_count != len(entries) or file_count != EXPECTED_FILE_COUNT:
        raise ManifestError(f"manifest must contain {EXPECTED_FILE_COUNT} files")
    if shard_count != len(actual_shards) or actual_shards != _expected_shards():
        raise ManifestError(
            f"manifest must contain {EXPECTED_SHARD_COUNT} weight shards"
        )
    if total_bytes != sum(entry["size"] for entry in entries):
        raise ManifestError("manifest total_bytes mismatch")
    if safetensors_bytes != sum(
        entry["size"] for entry in entries if entry["path"].endswith(".safetensors")
    ):
        raise ManifestError("manifest safetensors_bytes mismatch")


def _open_manifest_file(root_fd: int, path: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    current_fd = os.dup(root_fd)
    try:
        parts = path.split("/")
        for component in parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return os.open(parts[-1], flags, dir_fd=current_fd)
    finally:
        os.close(current_fd)


def _scan_snapshot(
    root_fd: int, expected_files: set[str], expected_directories: set[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    unexpected: set[str] = set()
    unsafe: set[str] = set()
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    pending = [("", os.dup(root_fd))]
    while pending:
        parent_path, directory_fd = pending.pop()
        try:
            try:
                with os.scandir(directory_fd) as entries:
                    names = sorted(entry.name for entry in entries)
            except OSError:
                unsafe.add(parent_path or "<snapshot>")
                continue
            for name in names:
                path = f"{parent_path}/{name}" if parent_path else name
                try:
                    details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError:
                    unsafe.add(path)
                    continue
                if path in expected_files:
                    continue
                if path in expected_directories:
                    if not stat.S_ISDIR(details.st_mode):
                        unsafe.add(path)
                        continue
                    try:
                        child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                    except OSError:
                        unsafe.add(path)
                        continue
                    if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                        os.close(child_fd)
                        unsafe.add(path)
                        continue
                    pending.append((path, child_fd))
                    continue
                if stat.S_ISDIR(details.st_mode):
                    unexpected.add(path)
                    try:
                        child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                    except OSError:
                        unsafe.add(path)
                        continue
                    if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                        os.close(child_fd)
                        unsafe.add(path)
                        continue
                    pending.append((path, child_fd))
                elif stat.S_ISREG(details.st_mode):
                    unexpected.add(path)
                else:
                    unsafe.add(path)
        finally:
            os.close(directory_fd)
    return tuple(sorted(unexpected)), tuple(sorted(unsafe))


def verify(
    manifest: Mapping[str, Any], snapshot_dir: str | os.PathLike[str]
) -> VerificationReport:
    """Hash every expected regular file without network access or symlink traversal."""

    validate_manifest(manifest)
    root_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_fd = os.open(os.fspath(snapshot_dir), root_flags)
    except FileNotFoundError:
        return VerificationReport(
            missing=tuple(entry["path"] for entry in manifest["files"]),
            changed=(),
            unsafe=(),
            unexpected=(),
            verified_files=0,
            verified_bytes=0,
        )
    except OSError:
        return VerificationReport((), (), ("<snapshot>",), (), 0, 0)

    missing: list[str] = []
    changed: list[str] = []
    unsafe: list[str] = []
    verified_files = 0
    verified_bytes = 0
    try:
        expected_files = {entry["path"] for entry in manifest["files"]}
        expected_directories = {
            "/".join(path.split("/")[:depth])
            for path in expected_files
            for depth in range(1, len(path.split("/")))
        }
        unexpected, scanned_unsafe = _scan_snapshot(
            root_fd, expected_files, expected_directories
        )
        unsafe.extend(scanned_unsafe)
        for entry in manifest["files"]:
            path = entry["path"]
            try:
                file_fd = _open_manifest_file(root_fd, path)
            except FileNotFoundError:
                missing.append(path)
                continue
            except OSError as error:
                if error.errno == errno.ENOENT:
                    missing.append(path)
                else:
                    unsafe.append(path)
                continue
            try:
                details = os.fstat(file_fd)
                if not stat.S_ISREG(details.st_mode):
                    unsafe.append(path)
                    continue
                if details.st_size != entry["size"]:
                    changed.append(path)
                    continue
                digest = hashlib.sha256()
                while chunk := os.read(file_fd, HASH_CHUNK_BYTES):
                    digest.update(chunk)
                final_details = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(final_details.st_mode)
                    or final_details.st_size != entry["size"]
                    or digest.hexdigest() != entry["sha256"]
                ):
                    changed.append(path)
                    continue
                verified_files += 1
                verified_bytes += entry["size"]
            finally:
                os.close(file_fd)
    finally:
        os.close(root_fd)
    return VerificationReport(
        missing=tuple(sorted(set(missing))),
        changed=tuple(sorted(set(changed))),
        unsafe=tuple(sorted(set(unsafe))),
        unexpected=unexpected,
        verified_files=verified_files,
        verified_bytes=verified_bytes,
    )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument("--repo", required=True)
    generate_parser.add_argument("--revision", required=True)
    generate_parser.add_argument("--output", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "generate":
        manifest = generate(args.repo, args.revision)
        _write_json(args.output, manifest)
        print(
            json.dumps(
                {
                    key: manifest[key]
                    for key in ("file_count", "total_bytes", "safetensors_bytes")
                },
                sort_keys=True,
            )
        )
        return 0

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = verify(manifest, args.snapshot)
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())

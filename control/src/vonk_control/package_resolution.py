"""Deterministic, metadata-only workload release resolution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from packaging.version import InvalidVersion, Version
from vonk_agent_protocol import PackageReleaseGraph, PackageReleaseLock

from cluster_profiles.workload_packages import PackageFamily

from .package_discovery import CandidateStore, DiscoveryCandidate

_DIGEST = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_MAX_DETAIL = 16


@dataclass(frozen=True)
class ResolutionResult:
    state: Literal["resolved", "unsupported", "incompatible", "quarantined"]
    lock: PackageReleaseLock | None = None
    reason_code: str | None = None
    detail: Mapping[str, object] = MappingProxyType({})


class ResolutionError(RuntimeError):
    """A selection or resolution operation cannot be completed safely."""

    def __init__(self, reason_code: str, detail: Mapping[str, object] | None = None):
        self.reason_code = reason_code
        self.detail = _detail(detail or {})
        super().__init__(reason_code)


def _detail(value: Mapping[str, object]) -> Mapping[str, object]:
    result: dict[str, object] = {}
    for key in sorted(value)[:_MAX_DETAIL]:
        if not isinstance(key, str):
            continue
        lowered = key.lower()
        item = (
            "[redacted]"
            if any(
                word in lowered
                for word in (
                    "secret",
                    "token",
                    "password",
                    "credential",
                    "authorization",
                )
            )
            else value[key]
        )
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = item if not isinstance(item, str) else item[:256]
        else:
            result[key] = "[redacted]"
    return MappingProxyType(result)


def _path(value: object, path: str) -> object | None:
    current = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a full sha256 digest")
    return value.removeprefix("sha256:")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _materialization(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        method = value.get("method")
    else:
        method = value
    mapping = {
        "file": "file",
        "archive": "archive",
        "oci-image": "oci-content",
        "oci-content": "oci-content",
        "python-wheel": "wheel",
        "wheel": "wheel",
        "executable": "executable",
        "configuration": "configuration",
        "snapshot": "snapshot",
    }
    if method not in mapping:
        raise ValueError("unsupported materialization method")
    return {"method": mapping[method]}


def _source(item: Mapping[str, object]) -> list[Mapping[str, object]]:
    sources = item.get("sources")
    if not isinstance(sources, (list, tuple)) or not sources:
        url = item.get("url")
        if isinstance(url, str):
            sources = [{"provider": "https", "url": url}]
    if not isinstance(sources, (list, tuple)) or not sources:
        raise ValueError("component has no source")
    normalized: list[Mapping[str, object]] = []
    for value in sources:
        if not isinstance(value, Mapping):
            raise TypeError("component source is invalid")
        provider = value.get("provider")
        if provider == "https":
            url = value.get("url")
            if (
                not isinstance(url, str)
                or not url.startswith("https://")
                or "?" in url
                or "#" in url
            ):
                raise ValueError("component source URL is invalid")
            normalized.append({"provider": "https", "url": url})
        elif provider == "oci":
            reference = value.get("reference")
            if not isinstance(reference, str) or "@sha256:" not in reference:
                raise ValueError("OCI source is not digest pinned")
            normalized.append({"provider": "oci", "reference": reference})
        elif provider == "git":
            repository, commit = value.get("repository"), value.get("commit")
            if (
                not isinstance(repository, str)
                or not repository.startswith("https://")
                or not isinstance(commit, str)
                or _COMMIT.fullmatch(commit) is None
            ):
                raise ValueError("Git source is not immutable")
            normalized.append(
                {"provider": "git", "repository": repository, "commit": commit}
            )
        elif provider == "huggingface":
            repository, revision = value.get("repository"), value.get("revision")
            if (
                not isinstance(repository, str)
                or not isinstance(revision, str)
                or _COMMIT.fullmatch(revision) is None
            ):
                raise ValueError("Hugging Face source is not immutable")
            normalized.append(
                {
                    "provider": "huggingface",
                    "repository": repository,
                    "revision": revision,
                }
            )
        elif provider in {"python-index", "signed-http-index"}:
            url, digest = value.get("url"), value.get("digest")
            if (
                not isinstance(url, str)
                or not url.startswith("https://")
                or not isinstance(digest, str)
                or not digest.startswith("sha256:")
            ):
                raise ValueError("index source is not immutable")
            normalized.append({"provider": provider, "url": url, "digest": digest})
        else:
            raise ValueError("component source provider is unsupported")
    if len(normalized) > 8:
        raise ValueError("component has too many sources")
    return normalized


def _component(
    template: Mapping[str, object], item: Mapping[str, object]
) -> dict[str, object]:
    merged = dict(template)
    merged.update(item)
    if (
        not isinstance(merged.get("name"), str)
        or not isinstance(merged.get("kind"), str)
        or not isinstance(merged.get("media_type"), str)
    ):
        raise TypeError("component layout is incomplete")
    digest = _digest(merged.get("digest"), f"component {merged['name']} digest")
    size = merged.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("component size is missing")
    unpacked = merged.get("unpacked_size")
    if unpacked is not None and (
        isinstance(unpacked, bool) or not isinstance(unpacked, int) or unpacked <= 0
    ):
        raise ValueError("component unpacked size is invalid")
    platforms = merged.get("platforms")
    if (
        not isinstance(platforms, (list, tuple))
        or not platforms
        or any(not isinstance(platform, str) for platform in platforms)
    ):
        raise ValueError("component platforms are missing")
    evidence = merged.get("evidence", ())
    if not isinstance(evidence, (list, tuple)):
        raise TypeError("component evidence is invalid")
    return {
        "name": merged["name"],
        "kind": merged["kind"],
        "media_type": merged["media_type"],
        "sources": _source(merged),
        "digest": "sha256:" + digest,
        "size": size,
        "unpacked_size": unpacked,
        "platforms": list(platforms),
        "materialization": _materialization(merged.get("materialization")),
        "evidence": [
            dict(record) for record in evidence if isinstance(record, Mapping)
        ],
    }


class PackageResolver:
    """Resolve one candidate against a typed family recipe and exact deps."""

    def __init__(
        self,
        candidates: CandidateStore,
        *,
        resolver_name: str = "metadata-v1",
        resolver_version: int = 1,
    ):
        self._candidates = candidates
        self._resolver_name = resolver_name
        self._resolver_version = resolver_version

    def candidates(
        self, family_id: str | None = None
    ) -> tuple[DiscoveryCandidate, ...]:
        return tuple(record.candidate for record in self._candidates.records(family_id))

    def resolve(
        self,
        candidate_id: str,
        family: PackageFamily,
        dependencies: Mapping[str, object],
    ) -> ResolutionResult:
        record = self._candidates.get(candidate_id)
        if record is None:
            return ResolutionResult(
                "unsupported",
                reason_code="candidate_not_found",
                detail=_detail({"candidate_id": candidate_id}),
            )
        candidate = record.candidate
        if candidate.family_id != family.family_id:
            return ResolutionResult(
                "unsupported", reason_code="candidate_family_mismatch"
            )
        if record.state == "quarantined":
            return ResolutionResult(
                "quarantined",
                reason_code=record.reason_code or "upstream_mutation",
                detail=record.detail,
            )
        metadata = candidate.metadata
        try:
            lock_document = self._lock_document(
                candidate, family, metadata, dependencies
            )
            lock = PackageReleaseLock.parse(lock_document)
            releases: dict[str, PackageReleaseLock] = {lock.digest: lock}
            for value in dependencies.values():
                if isinstance(value, PackageReleaseLock):
                    releases[value.digest] = value
            PackageReleaseGraph.resolve(lock.digest, releases)
        except (ValueError, TypeError) as error:
            message = str(error)
            if "cycle" in message:
                reason = "dependency_cycle"
            elif "missing" in message and "dependency" in message:
                reason = "dependency_missing"
            else:
                reason = (
                    "incomplete_checksum_metadata"
                    if "digest" in message or "size" in message
                    else "resolution_unsupported"
                )
            return ResolutionResult(
                "unsupported", reason_code=reason, detail=_detail({"error": message})
            )
        except (KeyError, IndexError, AttributeError, RuntimeError) as error:
            message = str(error)
            if "cycle" in message:
                reason = "dependency_cycle"
            elif "missing" in message or "dependency" in message:
                reason = "dependency_missing"
            else:
                reason = "resolution_unsupported"
            return ResolutionResult(
                "unsupported", reason_code=reason, detail=_detail({"error": message})
            )
        required = set(family.policy["required_evidence"])
        available = {record.get("kind") for record in lock.provenance}
        available.update(
            record.get("kind")
            for component in (*lock.components, lock.adapter)
            for record in component.evidence
        )
        if not required <= available:
            return ResolutionResult(
                "unsupported",
                reason_code="missing_evidence",
                detail=_detail({"missing": ",".join(sorted(required - available))}),
            )
        return ResolutionResult("resolved", lock=lock)

    def _lock_document(
        self,
        candidate: DiscoveryCandidate,
        family: PackageFamily,
        metadata: Mapping[str, object],
        dependencies: Mapping[str, object],
    ) -> dict[str, object]:
        release = metadata.get("release", {})
        if not isinstance(release, Mapping):
            release = {}
        components_value = metadata.get("components", ())
        if not isinstance(components_value, (list, tuple)):
            raise TypeError("components layout is unsupported")
        declared_dependencies = metadata.get("dependencies", {})
        if not isinstance(declared_dependencies, Mapping):
            raise TypeError("dependency layout is unsupported")
        expected_dependency_ids = {
            str(item["family_id"])
            for item in family.resolution["dependencies"]
            if isinstance(item, Mapping) and isinstance(item.get("family_id"), str)
        }
        unknown_dependencies = {
            str(key) for key in declared_dependencies
        } - expected_dependency_ids
        if unknown_dependencies:
            raise ValueError(
                "dependency is not declared by the family recipe: "
                + ",".join(sorted(unknown_dependencies))
            )
        by_name = {
            item.get("name"): item
            for item in components_value
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
        components: list[dict[str, object]] = []
        for template in family.resolution["components"]:
            if not isinstance(template, Mapping):
                raise TypeError("component template is invalid")
            name = template["name"]
            item = by_name.get(name)
            if not isinstance(item, Mapping):
                raise TypeError(f"component {name} is unsupported")
            components.append(_component(template, item))
        adapter_value = metadata.get("adapter")
        if not isinstance(adapter_value, Mapping):
            raise TypeError("adapter component is unsupported")
        adapter_template = {
            "kind": "adapter",
            "name": adapter_value.get("name", "adapter"),
            "materialization": "executable",
            "platforms": adapter_value.get("platforms", ["linux/arm64"]),
            "media_type": adapter_value.get(
                "media_type", "application/vnd.vonk-forge.workload-adapter.v1"
            ),
        }
        adapter = _component(adapter_template, adapter_value)
        dependency_digests: list[str] = []
        for template in family.resolution["dependencies"]:
            if not isinstance(template, Mapping):
                raise TypeError("dependency template is invalid")
            family_id = str(template["family_id"])
            value = _path(metadata, str(template["release_digest_binding"]))
            if value is None:
                value = dependencies.get(family_id)
            if isinstance(value, PackageReleaseLock):
                value = value.digest
            if isinstance(value, Mapping):
                value = value.get("digest")
            dependency_digests.append(_digest(value, f"dependency {family_id}"))
        compatibility = dict(family.compatibility)
        compatibility["architectures"] = [
            str(item).removeprefix("linux-") for item in compatibility["architectures"]
        ]
        compatibility["operating_systems"] = list(compatibility["operating_systems"])
        compatibility["minimum_storage_bytes"] = compatibility.pop("min_storage_bytes")
        if "min_memory_bytes" in compatibility:
            compatibility["minimum_memory_bytes"] = compatibility.pop("min_memory_bytes")
        if "cuda" in compatibility:
            compatibility["minimum_cuda"] = compatibility.pop("cuda")["minimum"]
        if "driver" in compatibility:
            compatibility["minimum_driver"] = compatibility.pop("driver")["minimum"]
        # Execution backend is part of the signed release lock.  Omitting it
        # silently selects the agent's historical native default and can run
        # an OCI/Python workload under the wrong sandbox.
        backend = family.execution.get("backend")
        if not isinstance(backend, str):
            raise TypeError("family execution backend is invalid")
        compatibility["required_capabilities"] = [
            "package-abi-v1",
            f"package-backend-{backend}-v1",
        ]
        compatibility["backends"] = [backend]
        # The authoring schema includes timeout policy; the wire lock carries
        # only the stable validation identity and optional evidence binding.
        validation = [
            {
                key: value
                for key, value in item.items()
                if key in {"kind", "component", "digest", "required"}
            }
            for item in family.validation
        ]
        provenance = metadata.get("provenance", ())
        if not isinstance(provenance, (list, tuple)):
            provenance = ()
        return {
            "schema_version": 1,
            "family_id": family.family_id,
            "upstream_version": candidate.upstream_version,
            "upstream_identity": dict(candidate.upstream_identity),
            "components": components,
            "dependency_digests": dependency_digests,
            "adapter": adapter,
            "adapter_abi": family.execution["adapter_abi"],
            "compatibility": compatibility,
            "validation": validation,
            "provenance": [
                dict(item) for item in provenance if isinstance(item, Mapping)
            ],
            "resolver": {
                "name": self._resolver_name,
                "version": self._resolver_version,
            },
        }

    def select_latest(
        self, family_id: str, family: PackageFamily
    ) -> tuple[DiscoveryCandidate, ...]:
        values = list(self.candidates(family_id))
        scheme = family.versions["scheme"]
        if scheme == "opaque":
            raise ResolutionError(
                "resolution_unsupported", {"reason": "opaque versions have no ordering"}
            )
        if scheme == "semver":

            def key(item: DiscoveryCandidate) -> tuple[int, int, int, int, str]:
                match = re.fullmatch(
                    r"v?(\d+)\.(\d+)\.(\d+)(?:-([^+]+))?(?:\+.*)?",
                    item.upstream_version,
                )
                if not match:
                    raise ResolutionError(
                        "resolution_unsupported", {"reason": "invalid SemVer"}
                    )
                return (
                    int(match[1]),
                    int(match[2]),
                    int(match[3]),
                    0 if match[4] is None else -1,
                    item.id,
                )

            return tuple(sorted(values, key=key, reverse=True))
        try:
            return tuple(
                sorted(
                    values,
                    key=lambda item: Version(item.upstream_version),
                    reverse=True,
                )
            )
        except InvalidVersion as error:
            raise ResolutionError(
                "resolution_unsupported", {"reason": "invalid PEP 440"}
            ) from error


__all__ = ["PackageResolver", "ResolutionError", "ResolutionResult"]

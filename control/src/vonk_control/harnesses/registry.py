"""Exact schema-v1 harness lookup and projection compilation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from vonk_agent_protocol.workload_packages import (
    SignedPackageObjectReceipt,
    package_object_receipt_signing_bytes,
)

from ..catalog_contract import (
    CatalogContractError,
    catalog_content_sha256,
    validate_catalog_document,
)
from ..source_bundles import (
    BundleManifest,
    GeneratedSourceBundle,
    SourceBundleError,
    SourceBundleStore,
)
from .common import HarnessCompileError, SyntheticHarnessCompiler, validate_projection
from .contracts import HarnessBinding, HarnessCompiler, HarnessProjection

BUILTIN_HARNESS_SLUGS = (
    "vllm",
    "sglang",
    "tensorrt-llm",
    "llama-cpp",
    "ds4",
    "diffusers",
    "comfyui",
    "pytorch-pipeline",
)

_BUNDLE_MEDIA_TYPE = "application/vnd.vonk-forge.source-bundle.v1+tar"
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class HarnessCompilerLoader(Protocol):
    """Load a compiler only from exact verified source bundle material."""

    def load(self, archive: bytes, manifest: BundleManifest) -> HarnessCompiler: ...


@dataclass(frozen=True, slots=True)
class VerifiedSourceBundle:
    digest: str
    archive_sha256: str
    archive: bytes
    manifest: BundleManifest
    signer: str


class SourceBundleReceiptVerifier:
    """Verify existing Ed25519 package-object receipts for exact archive bytes."""

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError("source bundle receipt public key is invalid")
        self._public_key = public_key
        self.signer = hashlib.sha256(public_key.public_bytes_raw()).hexdigest()

    def verify(
        self, bundle: GeneratedSourceBundle, receipt: object
    ) -> VerifiedSourceBundle:
        archive_sha256 = hashlib.sha256(bundle.archive).hexdigest()
        if type(receipt) is not SignedPackageObjectReceipt:
            raise HarnessCompileError("source bundle receipt is invalid")
        if (
            receipt.signature.key_id != self.signer
            or receipt.claims.object_digest != archive_sha256
            or receipt.claims.size != len(bundle.archive)
        ):
            raise HarnessCompileError(
                "source bundle receipt does not bind exact archive"
            )
        try:
            self._public_key.verify(
                bytes.fromhex(receipt.signature.value),
                package_object_receipt_signing_bytes(receipt.claims),
            )
        except (InvalidSignature, ValueError, TypeError) as error:
            raise HarnessCompileError(
                "source bundle receipt signature is invalid"
            ) from error
        return VerifiedSourceBundle(
            digest=bundle.sha256,
            archive_sha256=archive_sha256,
            archive=bundle.archive,
            manifest=bundle.manifest,
            signer=self.signer,
        )


class SourceBundleAuthority:
    """Load exact source bytes and verify them with the existing receipt authority."""

    def __init__(
        self, store: SourceBundleStore, verifier: SourceBundleReceiptVerifier
    ) -> None:
        if not isinstance(store, SourceBundleStore):
            raise TypeError("source bundle store is invalid")
        if not isinstance(verifier, SourceBundleReceiptVerifier):
            raise TypeError("source bundle receipt verifier is invalid")
        self._store = store
        self._verifier = verifier

    def verify(
        self, identity: Mapping[str, object], receipt: object
    ) -> VerifiedSourceBundle:
        digest, expected_bytes, _media_type = _bundle_identity(identity)
        try:
            bundle = self._store.get(digest)
        except SourceBundleError as error:
            raise HarnessCompileError("source bundle verification failed") from error
        if len(bundle.archive) != expected_bytes:
            raise HarnessCompileError("source bundle verification failed")
        verified = self._verifier.verify(bundle, receipt)
        if verified.digest != digest:
            raise HarnessCompileError("source bundle verification failed")
        return verified


@dataclass(frozen=True, slots=True)
class TrustedBuiltinComposition:
    """The only composition-root input accepted for reserved harness compilers."""

    compilers: tuple[HarnessCompiler, ...]


@dataclass(frozen=True, slots=True)
class _CustomRegistration:
    slug: str
    identity: tuple[str, int, str]
    archive_sha256: str
    signer: str
    receipt: object
    loader: HarnessCompilerLoader


class HarnessRegistry:
    def __init__(
        self, *, source_bundle_authority: SourceBundleAuthority | None = None
    ) -> None:
        self._compilers: dict[str, HarnessCompiler] = {}
        self._custom: dict[str, _CustomRegistration] = {}
        self._source_bundle_authority = source_bundle_authority

    @classmethod
    def with_builtins(cls) -> HarnessRegistry:
        return cls.from_trusted_builtins(
            TrustedBuiltinComposition(
                tuple(SyntheticHarnessCompiler(slug) for slug in BUILTIN_HARNESS_SLUGS)
            )
        )

    @classmethod
    def from_trusted_builtins(
        cls, composition: TrustedBuiltinComposition
    ) -> HarnessRegistry:
        if type(composition) is not TrustedBuiltinComposition:
            raise HarnessCompileError("trusted built-in composition is invalid")
        registry = cls()
        if len(composition.compilers) != len(BUILTIN_HARNESS_SLUGS):
            raise HarnessCompileError("trusted built-in composition is incomplete")
        for compiler in composition.compilers:
            registry._register_trusted_builtin(compiler)
        if tuple(sorted(registry._compilers)) != tuple(sorted(BUILTIN_HARNESS_SLUGS)):
            raise HarnessCompileError("trusted built-in composition is incomplete")
        return registry

    def _register_trusted_builtin(self, compiler: HarnessCompiler) -> None:
        _compiler_identity(compiler)
        if compiler.slug not in BUILTIN_HARNESS_SLUGS:
            raise HarnessCompileError("trusted built-in harness compiler is invalid")
        if compiler.slug in self._compilers:
            raise HarnessCompileError("harness compiler is already registered")
        self._compilers[compiler.slug] = compiler

    def register(
        self,
        loader: object,
        *,
        slug: str,
        source_bundle: Mapping[str, object],
        receipt: object = None,
    ) -> None:
        """Register a non-reserved compiler loaded solely from verified source bytes."""
        if not isinstance(slug, str) or not slug:
            raise HarnessCompileError("harness compiler slug is invalid")
        if slug in BUILTIN_HARNESS_SLUGS:
            raise HarnessCompileError(
                "reserved built-in harness slug cannot be registered"
            )
        if slug in self._compilers or slug in self._custom:
            raise HarnessCompileError("harness compiler is already registered")
        if self._source_bundle_authority is None or not isinstance(
            source_bundle, Mapping
        ):
            raise HarnessCompileError(
                "custom adapter requires a source bundle authority"
            )
        if not callable(getattr(loader, "load", None)):
            raise HarnessCompileError("harness compiler loader is invalid")
        identity = _bundle_identity(source_bundle)
        verified = self._source_bundle_authority.verify(source_bundle, receipt)
        compiler = _load_compiler(loader, verified)
        _require_compiler_identity(compiler, slug)
        self._custom[slug] = _CustomRegistration(
            slug=slug,
            identity=identity,
            archive_sha256=verified.archive_sha256,
            signer=verified.signer,
            receipt=receipt,
            loader=loader,
        )

    def compile(
        self,
        resolved_harness: Mapping[str, object] | object,
        recipe: Mapping[str, object],
        distribution: Mapping[str, object],
        patch: Mapping[str, object] | None,
        parameters: Mapping[str, object],
        topology: Mapping[str, object],
        role: str,
        rank: int,
    ) -> HarnessProjection:
        harness = _resolved_document(resolved_harness, "execution harness")
        slug, bundle = _harness_identity(harness)
        compiler = self._compiler_for(slug, bundle)
        runtime_distribution = _resolved_document(distribution, "runtime distribution")
        if (
            runtime_distribution.get("kind") != "runtime-distribution"
            or runtime_distribution.get("platform") != "linux/arm64"
        ):
            raise HarnessCompileError("runtime distribution must target linux/arm64")
        harness_identity = harness.get("identity")
        implemented_harness = runtime_distribution.get("implements_harness")
        harness_digest = _resolved_digest(resolved_harness, harness)
        if (
            not isinstance(harness_identity, Mapping)
            or not isinstance(implemented_harness, Mapping)
            or implemented_harness.get("publisher") != harness_identity.get("publisher")
            or implemented_harness.get("slug") != slug
            or implemented_harness.get("content_sha256") != harness_digest
        ):
            raise HarnessCompileError("runtime distribution does not implement harness")
        distribution_digest = _resolved_digest(distribution, runtime_distribution)
        node_count = (
            topology.get("node_count") if isinstance(topology, Mapping) else None
        )
        if (
            not isinstance(node_count, int)
            or isinstance(node_count, bool)
            or node_count < 1
            or not isinstance(role, str)
            or not role
            or not isinstance(rank, int)
            or isinstance(rank, bool)
            or not 0 <= rank < node_count
        ):
            raise HarnessCompileError("harness topology binding is invalid")
        projection = compiler.compile(
            recipe, runtime_distribution, patch, parameters, topology, role, rank
        )
        _require_compiler_identity(compiler, slug)
        if projection.slug != slug:
            raise HarnessCompileError("harness compiler projection identity is invalid")
        projection = replace(
            projection,
            binding=HarnessBinding(
                harness_content_sha256=harness_digest,
                distribution_content_sha256=distribution_digest,
                topology_node_count=node_count,
                role=role,
                rank=rank,
            ),
        )
        validate_projection(projection)
        return projection

    def _compiler_for(self, slug: str, bundle: Mapping[str, object]) -> HarnessCompiler:
        registered = self._custom.get(slug)
        if registered is not None:
            if (
                self._source_bundle_authority is None
                or _bundle_identity(bundle) != registered.identity
            ):
                raise HarnessCompileError(
                    "custom adapter lacks the exact signed source bundle"
                )
            verified = self._source_bundle_authority.verify(bundle, registered.receipt)
            if (
                verified.archive_sha256 != registered.archive_sha256
                or verified.signer != registered.signer
            ):
                raise HarnessCompileError("source bundle verification failed")
            compiler = _load_compiler(registered.loader, verified)
            _require_compiler_identity(compiler, registered.slug)
            return compiler
        compiler = self._compilers.get(slug)
        if compiler is None:
            raise HarnessCompileError("unknown execution harness")
        _require_compiler_identity(compiler, slug)
        return compiler


def _load_compiler(loader: object, bundle: VerifiedSourceBundle) -> HarnessCompiler:
    try:
        compiler = loader.load(bundle.archive, bundle.manifest)
    except HarnessCompileError:
        raise
    except Exception as error:
        raise HarnessCompileError("harness compiler loader failed") from error
    _compiler_identity(compiler)
    return compiler


def _compiler_identity(compiler: object) -> HarnessCompiler:
    if (
        not isinstance(getattr(compiler, "slug", None), str)
        or not compiler.slug
        or getattr(compiler, "contract_version", None) != 1
        or not callable(getattr(compiler, "compile", None))
    ):
        raise HarnessCompileError("harness compiler identity is invalid")
    return compiler


def _require_compiler_identity(compiler: object, slug: str) -> None:
    _compiler_identity(compiler)
    if compiler.slug != slug:
        raise HarnessCompileError("harness compiler source identity is invalid")


def _resolved_document(
    value: Mapping[str, object] | object, label: str
) -> Mapping[str, object]:
    document = value if isinstance(value, Mapping) else getattr(value, "document", None)
    if not isinstance(document, Mapping):
        raise HarnessCompileError(f"resolved {label} is invalid")
    try:
        validate_catalog_document(document)
    except CatalogContractError as error:
        raise HarnessCompileError(f"resolved {label} is invalid") from error
    return document


def _resolved_digest(
    value: Mapping[str, object] | object, document: Mapping[str, object]
) -> str:
    digest = catalog_content_sha256(document)
    revision_digest = getattr(value, "content_sha256", None)
    if revision_digest is not None and revision_digest != digest:
        raise HarnessCompileError("resolved catalog identity is invalid")
    return digest


def _harness_identity(
    document: Mapping[str, object],
) -> tuple[str, Mapping[str, object]]:
    identity = document.get("identity")
    bundle = document.get("source_bundle")
    if (
        document.get("schema_version") != 1
        or isinstance(document.get("schema_version"), bool)
        or document.get("kind") != "execution-harness"
        or document.get("runtime_interface") != "vonk.runtime.v1"
        or not isinstance(identity, Mapping)
        or not isinstance(identity.get("slug"), str)
        or not isinstance(bundle, Mapping)
    ):
        raise HarnessCompileError("resolved execution harness is invalid")
    _bundle_identity(bundle)
    return identity["slug"], bundle


def _bundle_identity(bundle: Mapping[str, object]) -> tuple[str, int, str]:
    if set(bundle) != {"sha256", "expected_bytes", "media_type"}:
        raise HarnessCompileError("harness source bundle is invalid")
    digest = bundle.get("sha256")
    expected_bytes = bundle.get("expected_bytes")
    media_type = bundle.get("media_type")
    if (
        not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes < 1
        or media_type != _BUNDLE_MEDIA_TYPE
    ):
        raise HarnessCompileError("harness source bundle is invalid")
    return digest, expected_bytes, media_type

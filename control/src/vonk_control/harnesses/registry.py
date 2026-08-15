"""Exact schema-v1 harness lookup and projection compilation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from vonk_agent_protocol.workload_packages import (
    SignedPackageObjectReceipt,
    package_object_receipt_signing_bytes,
)

from ..catalog_contract import CatalogContractError, validate_catalog_document
from ..source_bundles import GeneratedSourceBundle, SourceBundleError, SourceBundleStore
from .common import HarnessCompileError, SyntheticHarnessCompiler, validate_projection
from .contracts import HarnessCompiler, HarnessProjection

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


@dataclass(frozen=True, slots=True)
class VerifiedSourceBundle:
    digest: str
    archive_bytes: int
    signer: str


class SourceBundleReceiptVerifier:
    """Verify existing signed package-object receipts for a source bundle."""

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError("source bundle receipt public key is invalid")
        self._public_key = public_key
        self.signer = (
            __import__("hashlib").sha256(public_key.public_bytes_raw()).hexdigest()
        )

    def verify(
        self, bundle: GeneratedSourceBundle, receipt: object
    ) -> VerifiedSourceBundle:
        if type(receipt) is not SignedPackageObjectReceipt:
            raise HarnessCompileError("source bundle receipt is invalid")
        if (
            receipt.signature.key_id != self.signer
            or receipt.claims.object_digest != bundle.sha256
            or receipt.claims.size != len(bundle.archive)
        ):
            raise HarnessCompileError("source bundle receipt does not bind the bundle")
        try:
            self._public_key.verify(
                bytes.fromhex(receipt.signature.value),
                package_object_receipt_signing_bytes(receipt.claims),
            )
        except (InvalidSignature, ValueError, TypeError) as error:
            raise HarnessCompileError(
                "source bundle receipt signature is invalid"
            ) from error
        return VerifiedSourceBundle(bundle.sha256, len(bundle.archive), self.signer)


class SourceBundleAuthority:
    """Load exact source bytes and verify their existing signed receipt."""

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
        return self._verifier.verify(bundle, receipt)


@dataclass(frozen=True, slots=True)
class _CustomRegistration:
    identity: tuple[str, int, str]
    receipt: object
    verified: VerifiedSourceBundle


class HarnessRegistry:
    def __init__(
        self, *, source_bundle_authority: SourceBundleAuthority | None = None
    ) -> None:
        self._compilers: dict[str, HarnessCompiler] = {}
        self._custom: dict[str, _CustomRegistration] = {}
        self._source_bundle_authority = source_bundle_authority

    @classmethod
    def with_builtins(cls) -> HarnessRegistry:
        registry = cls()
        for slug in BUILTIN_HARNESS_SLUGS:
            registry._register_trusted_builtin(SyntheticHarnessCompiler(slug))
        return registry

    def _register_trusted_builtin(self, compiler: HarnessCompiler) -> None:
        if (
            type(compiler) is not SyntheticHarnessCompiler
            or compiler.slug not in BUILTIN_HARNESS_SLUGS
        ):
            raise HarnessCompileError("trusted built-in harness compiler is invalid")
        self._compilers[compiler.slug] = compiler

    def register(
        self,
        compiler: HarnessCompiler,
        *,
        source_bundle: Mapping[str, object] | None = None,
        receipt: object = None,
    ) -> None:
        if not isinstance(compiler.slug, str) or not compiler.slug:
            raise HarnessCompileError("harness compiler slug is invalid")
        if compiler.slug in BUILTIN_HARNESS_SLUGS:
            raise HarnessCompileError(
                "reserved built-in harness slug cannot be registered"
            )
        if compiler.contract_version != 1:
            raise HarnessCompileError("harness contract version is invalid")
        if compiler.slug in self._compilers:
            raise HarnessCompileError("harness compiler is already registered")
        if self._source_bundle_authority is None or not isinstance(
            source_bundle, Mapping
        ):
            raise HarnessCompileError(
                "custom adapter requires a source bundle authority"
            )
        identity = _bundle_identity(source_bundle)
        verified = self._source_bundle_authority.verify(source_bundle, receipt)
        if (verified.digest, verified.archive_bytes) != identity[:2]:
            raise HarnessCompileError("source bundle verification failed")
        if (
            getattr(compiler, "source_bundle_digest", None) != verified.digest
            or getattr(compiler, "source_bundle_signer", None) != verified.signer
        ):
            raise HarnessCompileError("compiler source bundle identity is invalid")
        self._compilers[compiler.slug] = compiler
        self._custom[compiler.slug] = _CustomRegistration(identity, receipt, verified)

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
        compiler = self._compilers.get(slug)
        if compiler is None:
            raise HarnessCompileError("unknown execution harness")
        registered = self._custom.get(slug)
        if registered is not None:
            if (
                self._source_bundle_authority is None
                or _bundle_identity(bundle) != registered.identity
            ):
                raise HarnessCompileError(
                    "custom adapter lacks the exact signed source bundle"
                )
            rechecked = self._source_bundle_authority.verify(bundle, registered.receipt)
            if rechecked != registered.verified:
                raise HarnessCompileError("source bundle verification failed")
        runtime_distribution = _resolved_document(distribution, "runtime distribution")
        if (
            runtime_distribution.get("kind") != "runtime-distribution"
            or runtime_distribution.get("platform") != "linux/arm64"
        ):
            raise HarnessCompileError("runtime distribution must target linux/arm64")
        harness_identity = harness.get("identity")
        implemented_harness = runtime_distribution.get("implements_harness")
        if (
            not isinstance(harness_identity, Mapping)
            or not isinstance(implemented_harness, Mapping)
            or implemented_harness.get("publisher") != harness_identity.get("publisher")
            or implemented_harness.get("slug") != slug
        ):
            raise HarnessCompileError("runtime distribution does not implement harness")
        projection = compiler.compile(
            recipe, runtime_distribution, patch, parameters, topology, role, rank
        )
        if projection.slug != slug:
            raise HarnessCompileError("harness compiler projection identity is invalid")
        validate_projection(projection)
        return projection


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

"""Exact schema-v1 harness lookup and projection compilation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from vonk_agent_protocol.workload_packages import (
    PackageHelperSignature,
    PackageObjectReceiptClaims,
    SignedPackageObjectReceipt,
    package_object_receipt_signing_bytes,
)

from ..catalog_contract import (
    CatalogContractError,
    canonical_catalog_document,
    catalog_content_sha256,
    parse_catalog_json,
    validate_catalog_document,
)
from ..source_bundles import (
    BundleManifest,
    SourceBundleError,
    SourceBundleStore,
)
from .common import (
    HarnessCompileError,
    SyntheticHarnessCompiler,
    custom_adapter_command,
    validate_projection,
)
from .contracts import HarnessBinding, HarnessCompiler, HarnessMount, HarnessProjection

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

_ADAPTER_PATH = "harness-adapter-v1.json"
_BUNDLE_MEDIA_TYPE = "application/vnd.vonk-forge.source-bundle.v1+tar"
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_INTERFACES = frozenset(
    {"openai", "image-job", "audio-job", "video-job", "mesh-job", "artifact-job"}
)


@dataclass(frozen=True, slots=True)
class _VerifiedSourceBundle:
    digest: str
    archive_sha256: str
    archive: bytes
    files: Mapping[str, bytes]
    manifest: BundleManifest
    signer: str


@dataclass(frozen=True, slots=True)
class TrustedBuiltinComposition:
    """The only composition-root input accepted for reserved harness compilers."""

    compilers: tuple[HarnessCompiler, ...]


@dataclass(frozen=True, slots=True)
class _AdapterSpec:
    digest: str
    slug: str
    argv_template: tuple[str, ...]
    allowed_parameters: frozenset[str]
    allowed_environment: frozenset[str]
    model_mount: HarnessMount
    output_mount: HarnessMount
    minimum_nodes: int
    maximum_nodes: int
    roles: frozenset[str]
    interfaces: tuple[str, ...]
    image: str
    user: str


@dataclass(frozen=True, slots=True)
class _BundleHarnessCompiler:
    """Sealed trusted-code compiler reconstructed from one signed adapter document."""

    spec: _AdapterSpec
    contract_version: int = 1

    @property
    def slug(self) -> str:
        return self.spec.slug

    def compile(
        self,
        recipe: Mapping[str, object],
        distribution: Mapping[str, object],
        patch: Mapping[str, object] | None,
        parameters: Mapping[str, object],
        topology: Mapping[str, object],
        role: str,
        rank: int,
    ) -> HarnessProjection:
        if not isinstance(parameters, Mapping) or any(
            not isinstance(name, str) or name not in self.spec.allowed_parameters
            for name in parameters
        ):
            raise HarnessCompileError("adapter parameters are not allowed")
        runtime = recipe.get("runtime")
        environment = (
            runtime.get("environment", []) if isinstance(runtime, Mapping) else []
        )
        if not isinstance(environment, Sequence) or isinstance(
            environment, (str, bytes)
        ):
            raise HarnessCompileError("adapter environment is invalid")
        if any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("name"), str)
            or item["name"] not in self.spec.allowed_environment
            for item in environment
        ):
            raise HarnessCompileError("adapter environment is not allowed")
        node_count = (
            topology.get("node_count") if isinstance(topology, Mapping) else None
        )
        if (
            type(node_count) is not int
            or not self.spec.minimum_nodes <= node_count <= self.spec.maximum_nodes
            or role not in self.spec.roles
            or type(rank) is not int
            or not 0 <= rank < node_count
        ):
            raise HarnessCompileError("adapter topology is invalid")
        expected_security = {
            "network_mode": "none",
            "user": self.spec.user,
            "no_new_privileges": True,
            "capabilities": [],
        }
        if (
            distribution.get("platform") != "linux/arm64"
            or distribution.get("image") != self.spec.image
            or distribution.get("security") != expected_security
        ):
            raise HarnessCompileError("adapter distribution expectation is invalid")
        return HarnessProjection(
            slug=self.slug,
            contract_version=self.contract_version,
            command=self.spec.argv_template,
            image=self.spec.image,
            network_mode="none",
            architecture="linux/arm64",
            user=self.spec.user,
            no_new_privileges=True,
            capabilities=(),
            model_mounts=(self.spec.model_mount,),
            output_mount=self.spec.output_mount,
        )


@dataclass(frozen=True, slots=True)
class _CustomRegistration:
    slug: str
    identity: tuple[str, int, str]
    archive_sha256: str
    signer: str
    receipt: SignedPackageObjectReceipt
    adapter_sha256: str


class HarnessRegistry:
    def __init__(
        self,
        *,
        source_bundle_store: SourceBundleStore | None = None,
        trusted_signer_keys: Mapping[str, bytes | Ed25519PublicKey] | None = None,
    ) -> None:
        if (
            source_bundle_store is not None
            and type(source_bundle_store) is not SourceBundleStore
        ):
            raise TypeError("source bundle store must be an exact SourceBundleStore")
        if (source_bundle_store is None) != (trusted_signer_keys is None):
            raise TypeError(
                "source bundle store and trusted signer keys must be configured together"
            )
        trusted_keys = _trusted_signer_key_data(trusted_signer_keys)
        if source_bundle_store is not None and not trusted_keys:
            raise TypeError("source bundle trusted signer keys are required")
        self._compilers: dict[str, HarnessCompiler] = {}
        self._custom: dict[str, _CustomRegistration] = {}
        self._source_bundle_store = source_bundle_store
        self._trusted_signer_keys = trusted_keys

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
        self, *, source_bundle: Mapping[str, object], receipt: object = None
    ) -> None:
        """Register only the repository-owned declarative signed-bundle adapter."""
        if self._source_bundle_store is None or not isinstance(source_bundle, Mapping):
            raise HarnessCompileError(
                "custom adapter requires a concrete source bundle store"
            )
        identity = _bundle_identity(source_bundle)
        stored_receipt = _source_bundle_receipt(receipt)
        verified = self._verify_source_bundle(source_bundle, stored_receipt)
        spec = _parse_adapter_spec(verified)
        if spec.slug in BUILTIN_HARNESS_SLUGS:
            raise HarnessCompileError(
                "reserved built-in harness slug cannot be registered"
            )
        if spec.slug in self._compilers or spec.slug in self._custom:
            raise HarnessCompileError("harness compiler is already registered")
        self._custom[spec.slug] = _CustomRegistration(
            slug=spec.slug,
            identity=identity,
            archive_sha256=verified.archive_sha256,
            signer=verified.signer,
            receipt=stored_receipt,
            adapter_sha256=spec.digest,
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
        if isinstance(compiler, _BundleHarnessCompiler):
            adapters = harness.get("adapters")
            if (
                not isinstance(adapters, list)
                or tuple(adapters) != compiler.spec.interfaces
            ):
                raise HarnessCompileError("adapter interface expectation is invalid")
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
            type(node_count) is not int
            or node_count < 1
            or not isinstance(role, str)
            or not role
            or type(rank) is not int
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
                self._source_bundle_store is None
                or _bundle_identity(bundle) != registered.identity
            ):
                raise HarnessCompileError(
                    "custom adapter lacks the exact signed source bundle"
                )
            verified = self._verify_source_bundle(bundle, registered.receipt)
            if (
                verified.archive_sha256 != registered.archive_sha256
                or verified.signer != registered.signer
            ):
                raise HarnessCompileError("source bundle verification failed")
            spec = _parse_adapter_spec(verified)
            if spec.slug != registered.slug or spec.digest != registered.adapter_sha256:
                raise HarnessCompileError("custom adapter document identity is invalid")
            return _BundleHarnessCompiler(spec)
        compiler = self._compilers.get(slug)
        if compiler is None:
            raise HarnessCompileError("unknown execution harness")
        _require_compiler_identity(compiler, slug)
        return compiler

    def _verify_source_bundle(
        self,
        identity: Mapping[str, object],
        receipt: SignedPackageObjectReceipt,
    ) -> _VerifiedSourceBundle:
        store = self._source_bundle_store
        if store is None:
            raise HarnessCompileError("source bundle verification failed")
        digest, expected_bytes, _media_type = _bundle_identity(identity)
        try:
            bundle = SourceBundleStore.get(store, digest)
        except SourceBundleError as error:
            raise HarnessCompileError("source bundle verification failed") from error
        archive_sha256 = hashlib.sha256(bundle.archive).hexdigest()
        if len(bundle.archive) != expected_bytes or bundle.sha256 != digest:
            raise HarnessCompileError("source bundle verification failed")
        key_bytes = self._trusted_signer_keys.get(receipt.signature.key_id)
        if key_bytes is None:
            raise HarnessCompileError("source bundle receipt signer is not trusted")
        if receipt.claims.object_digest != archive_sha256 or receipt.claims.size != len(
            bundle.archive
        ):
            raise HarnessCompileError(
                "source bundle receipt does not bind exact archive"
            )
        try:
            Ed25519PublicKey.from_public_bytes(key_bytes).verify(
                bytes.fromhex(receipt.signature.value),
                package_object_receipt_signing_bytes(receipt.claims),
            )
        except (InvalidSignature, ValueError, TypeError) as error:
            raise HarnessCompileError(
                "source bundle receipt signature is invalid"
            ) from error
        return _VerifiedSourceBundle(
            digest=bundle.sha256,
            archive_sha256=archive_sha256,
            archive=bundle.archive,
            files=bundle.files,
            manifest=bundle.manifest,
            signer=receipt.signature.key_id,
        )


def _trusted_signer_key_data(
    value: Mapping[str, bytes | Ed25519PublicKey] | None,
) -> Mapping[str, bytes]:
    if value is None:
        return MappingProxyType({})
    if type(value) not in (dict, _MAPPING_PROXY_TYPE):
        raise TypeError("trusted signer keys must be an inert concrete mapping")
    normalized: dict[str, bytes] = {}
    for key_id, public_key in value.items():
        if type(key_id) is not str or _DIGEST.fullmatch(key_id) is None:
            raise TypeError("trusted signer key ID is invalid")
        if type(public_key) is bytes:
            key_bytes = bytes(public_key)
        elif isinstance(public_key, Ed25519PublicKey):
            key_bytes = public_key.public_bytes_raw()
        else:
            raise TypeError("trusted signer public key is invalid")
        try:
            Ed25519PublicKey.from_public_bytes(key_bytes)
        except (TypeError, ValueError) as error:
            raise TypeError("trusted signer public key is invalid") from error
        if hashlib.sha256(key_bytes).hexdigest() != key_id:
            raise TypeError("trusted signer key ID does not match public key")
        normalized[key_id] = key_bytes
    return MappingProxyType(normalized)


def _source_bundle_receipt(receipt: object) -> SignedPackageObjectReceipt:
    if type(receipt) is not SignedPackageObjectReceipt:
        raise HarnessCompileError("source bundle receipt is invalid")
    try:
        claims = receipt.claims
        signature = receipt.signature
        return SignedPackageObjectReceipt(
            claims=PackageObjectReceiptClaims(
                schema_version=claims.schema_version,
                authority=claims.authority,
                object_digest=claims.object_digest,
                size=claims.size,
                relative_name=claims.relative_name,
            ),
            signature=PackageHelperSignature(
                algorithm=signature.algorithm,
                key_id=signature.key_id,
                value=signature.value,
            ),
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise HarnessCompileError("source bundle receipt is invalid") from error


def _parse_adapter_spec(bundle: _VerifiedSourceBundle) -> _AdapterSpec:
    manifest_file = tuple(
        item for item in bundle.manifest.files if item.path == _ADAPTER_PATH
    )
    payload = bundle.files.get(_ADAPTER_PATH)
    if len(manifest_file) != 1 or not isinstance(payload, bytes):
        raise HarnessCompileError("source bundle adapter document is invalid")
    if (
        manifest_file[0].size != len(payload)
        or manifest_file[0].sha256 != hashlib.sha256(payload).hexdigest()
    ):
        raise HarnessCompileError("source bundle adapter document is invalid")
    try:
        document = parse_catalog_json(payload)
    except CatalogContractError as error:
        raise HarnessCompileError(
            "source bundle adapter document is invalid"
        ) from error
    if canonical_catalog_document(document) != payload:
        raise HarnessCompileError("source bundle adapter document is noncanonical")
    if set(document) != {
        "schema_version",
        "slug",
        "contract_version",
        "argv_template",
        "allowed_parameters",
        "allowed_environment",
        "mounts",
        "topology",
        "interfaces",
        "distribution",
    }:
        raise HarnessCompileError("source bundle adapter document is invalid")
    schema_version = document.get("schema_version")
    contract_version = document.get("contract_version")
    slug = document.get("slug")
    if (
        type(schema_version) is not int
        or schema_version != 1
        or type(contract_version) is not int
        or contract_version != 1
        or not isinstance(slug, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", slug)
    ):
        raise HarnessCompileError("source bundle adapter document is invalid")
    argv = _string_tuple(document.get("argv_template"), "adapter argv template")
    try:
        argv = custom_adapter_command(argv)
    except HarnessCompileError as error:
        raise HarnessCompileError("adapter argv template is invalid") from error
    parameters = frozenset(
        _string_tuple(document.get("allowed_parameters"), "adapter parameters")
    )
    environment = frozenset(
        _string_tuple(document.get("allowed_environment"), "adapter environment")
    )
    mounts = document.get("mounts")
    topology = document.get("topology")
    interfaces = _string_tuple(document.get("interfaces"), "adapter interfaces")
    distribution = document.get("distribution")
    if (
        not isinstance(mounts, Mapping)
        or set(mounts) != {"models", "outputs"}
        or not isinstance(topology, Mapping)
        or set(topology) != {"minimum_nodes", "maximum_nodes", "roles"}
        or not interfaces
        or len(set(interfaces)) != len(interfaces)
        or len(parameters)
        != len(_string_tuple(document.get("allowed_parameters"), "adapter parameters"))
        or len(environment)
        != len(
            _string_tuple(document.get("allowed_environment"), "adapter environment")
        )
        or any(interface not in _INTERFACES for interface in interfaces)
        or not isinstance(distribution, Mapping)
        or set(distribution) != {"platform", "image", "security"}
    ):
        raise HarnessCompileError("source bundle adapter document is invalid")
    model_mount = _adapter_mount(mounts["models"], model=True)
    output_mount = _adapter_mount(mounts["outputs"], model=False)
    minimum_nodes = topology.get("minimum_nodes")
    maximum_nodes = topology.get("maximum_nodes")
    roles = _string_tuple(topology.get("roles"), "adapter roles")
    if (
        type(minimum_nodes) is not int
        or type(maximum_nodes) is not int
        or minimum_nodes < 1
        or maximum_nodes < minimum_nodes
        or not roles
        or len(set(roles)) != len(roles)
    ):
        raise HarnessCompileError("source bundle adapter topology is invalid")
    security = distribution["security"]
    image = distribution["image"]
    user = security.get("user") if isinstance(security, Mapping) else None
    if (
        distribution["platform"] != "linux/arm64"
        or not isinstance(image, str)
        or not isinstance(user, str)
        or not isinstance(security, Mapping)
        or type(security.get("no_new_privileges")) is not bool
        or security
        != {
            "network_mode": "none",
            "user": user,
            "no_new_privileges": True,
            "capabilities": [],
        }
    ):
        raise HarnessCompileError("source bundle adapter distribution is invalid")
    try:
        validate_projection(
            HarnessProjection(
                slug=slug,
                contract_version=contract_version,
                command=argv,
                image=image,
                network_mode="none",
                architecture="linux/arm64",
                user=user,
                no_new_privileges=True,
                capabilities=(),
                model_mounts=(model_mount,),
                output_mount=output_mount,
                binding=HarnessBinding("0" * 64, "0" * 64, minimum_nodes, roles[0], 0),
            )
        )
    except HarnessCompileError as error:
        raise HarnessCompileError(
            "source bundle adapter projection is invalid"
        ) from error
    return _AdapterSpec(
        digest=hashlib.sha256(payload).hexdigest(),
        slug=slug,
        argv_template=argv,
        allowed_parameters=parameters,
        allowed_environment=environment,
        model_mount=model_mount,
        output_mount=output_mount,
        minimum_nodes=minimum_nodes,
        maximum_nodes=maximum_nodes,
        roles=frozenset(roles),
        interfaces=interfaces,
        image=image,
        user=user,
    )


def _adapter_mount(value: object, *, model: bool) -> HarnessMount:
    expected = {"source", "target", "read_only"}
    if not model:
        expected = {*expected, "isolated"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise HarnessCompileError("source bundle adapter mount is invalid")
    source = value.get("source")
    target = value.get("target")
    read_only = value.get("read_only")
    isolated = value.get("isolated", False)
    if (
        not isinstance(source, str)
        or not isinstance(target, str)
        or type(read_only) is not bool
        or read_only is not model
        or type(isolated) is not bool
        or isolated is not (not model)
    ):
        raise HarnessCompileError("source bundle adapter mount is invalid")
    return HarnessMount(source, target, read_only=read_only, isolated=isolated)


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise HarnessCompileError(f"{label} is invalid")
    return tuple(value)


def _compiler_identity(compiler: object) -> HarnessCompiler:
    if (
        not isinstance(getattr(compiler, "slug", None), str)
        or not compiler.slug
        or type(getattr(compiler, "contract_version", None)) is not int
        or compiler.contract_version != 1
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
        type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
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
        or type(expected_bytes) is not int
        or expected_bytes < 1
        or media_type != _BUNDLE_MEDIA_TYPE
    ):
        raise HarnessCompileError("harness source bundle is invalid")
    return digest, expected_bytes, media_type

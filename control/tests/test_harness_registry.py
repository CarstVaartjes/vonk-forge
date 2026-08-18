from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from vonk_control.catalog_contract import catalog_content_sha256
from vonk_control.harnesses import BUILTIN_HARNESS_SLUGS, HarnessCompileError
from vonk_control.harnesses.common import (
    SyntheticHarnessCompiler,
    custom_adapter_command,
    structured_command,
    validate_projection,
)
from vonk_control.harnesses.registry import (
    HarnessRegistry,
    TrustedBuiltinComposition,
)
from vonk_control.workload_helper_authority import WorkloadObjectReceiptIssuer
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle


def harness_document(
    slug: str, *, source_bundle: dict[str, object] | None = None
) -> dict[str, object]:
    document = {
        "schema_version": 1,
        "kind": "execution-harness",
        "identity": {"publisher": "vonk-forge", "slug": slug},
        "metadata": {
            "title": f"{slug} harness",
            "description": "Synthetic harness used only by conformance tests.",
            "tags": ["synthetic"],
        },
        "runtime_interface": "vonk.runtime.v1",
        "adapters": ["openai"],
    }
    if slug in BUILTIN_HARNESS_SLUGS and source_bundle is None:
        document.update(
            {
                "compiler_slug": slug,
                "contract_version": 1,
                "topology_modes": ["single"],
                "capability_requirements": ["nvidia-gpu"],
                "security_exceptions": [],
            }
        )
    else:
        document["source_bundle"] = source_bundle or {
            "sha256": "a" * 64,
            "expected_bytes": 2048,
            "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
        }
    return document


def distribution_document(harness_slug: str = "vllm") -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "runtime-distribution",
        "identity": {"publisher": "vonk-forge", "slug": "synthetic-arm64"},
        "metadata": {
            "title": "Synthetic ARM64 runtime",
            "description": "Digest-pinned offline runtime for conformance tests.",
            "tags": ["synthetic"],
        },
        "implements_harness": {
            "kind": "execution-harness",
            "publisher": "vonk-forge",
            "slug": harness_slug,
            "content_sha256": "b" * 64,
        },
        "platform": "linux/arm64",
        "image": "registry.example/vonk/synthetic@sha256:" + "c" * 64,
        "security": {
            "network_mode": "none",
            "user": "10001:10001",
            "no_new_privileges": True,
            "capabilities": [],
        },
    }


def compile_harness(registry: HarnessRegistry, document: dict[str, object]):
    identity = document["identity"]
    assert isinstance(identity, dict)
    distribution = distribution_document(str(identity["slug"]))
    implements_harness = distribution["implements_harness"]
    assert isinstance(implements_harness, dict)
    implements_harness["content_sha256"] = catalog_content_sha256(document)
    recipe = recipe_document(document, distribution)
    topology = recipe["topology"]
    assert isinstance(topology, dict)
    return registry.compile(
        document,
        recipe=recipe,
        distribution=distribution,
        patch=None,
        parameters={},
        topology=topology,
        role="entrypoint",
        rank=0,
    )


def recipe_document(
    harness: dict[str, object], distribution: dict[str, object]
) -> dict[str, object]:
    harness_identity = harness["identity"]
    distribution_identity = distribution["identity"]
    assert isinstance(harness_identity, dict)
    assert isinstance(distribution_identity, dict)
    return {
        "execution": {
            "harness": {
                "kind": "execution-harness",
                "publisher": harness_identity["publisher"],
                "slug": harness_identity["slug"],
                "content_sha256": catalog_content_sha256(harness),
            },
            "patch_bundle": None,
        },
        "runtime": {
            "distribution": {
                "kind": "runtime-distribution",
                "publisher": distribution_identity["publisher"],
                "slug": distribution_identity["slug"],
                "content_sha256": catalog_content_sha256(distribution),
            },
            "entrypoint": ["serve", "--model", "/models"],
            "environment": [],
            "security": {"devices": ["nvidia.com/gpu=all"]},
        },
        "interfaces": [{"adapter": "openai"}],
        "topology": {
            "mode": "single",
            "node_count": 1,
            "parallelism": {
                "world_size": 1,
                "tensor": 1,
                "pipeline": 1,
                "data": 1,
                "backend": "local",
            },
            "fabric": {
                "connectivity": "none",
                "minimum_bandwidth_mbps": 0,
            },
            "roles": [{"name": "entrypoint", "count": 1}],
        },
    }


def synthetic_registry() -> HarnessRegistry:
    return HarnessRegistry.from_trusted_builtins(
        TrustedBuiltinComposition(
            tuple(SyntheticHarnessCompiler(slug) for slug in BUILTIN_HARNESS_SLUGS)
        )
    )


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_registry_compiles_each_trusted_builtin_harness_slug(slug: str) -> None:
    projection = compile_harness(synthetic_registry(), harness_document(slug))

    assert projection.slug == slug
    assert projection.command == ("serve", "--model", "/models")
    assert projection.image.endswith("@sha256:" + "c" * 64)
    assert projection.network_mode == "none"
    assert projection.architecture == "linux/arm64"
    assert projection.user == "10001:10001"
    assert projection.no_new_privileges is True
    assert projection.capabilities == ()
    assert all(mount.read_only for mount in projection.model_mounts)
    assert projection.output_mount.isolated is True
    assert projection.output_mount.read_only is False


def test_registry_fails_closed_for_an_unknown_builtin_slug() -> None:
    with pytest.raises(HarnessCompileError, match="unknown execution harness"):
        compile_harness(synthetic_registry(), harness_document("unknown-harness"))


def test_registry_rejects_a_distribution_for_a_different_harness() -> None:
    with pytest.raises(HarnessCompileError, match="does not implement harness"):
        harness = harness_document("sglang")
        distribution = distribution_document("vllm")
        recipe = recipe_document(harness, distribution)
        synthetic_registry().compile(
            harness,
            recipe=recipe,
            distribution=distribution,
            patch=None,
            parameters={},
            topology=recipe["topology"],
            role="entrypoint",
            rank=0,
        )


@pytest.mark.parametrize("mutation", ["metadata", "adapters", "extra"])
def test_registry_uses_the_canonical_execution_harness_schema(mutation: str) -> None:
    document = harness_document("vllm")
    if mutation == "extra":
        document["unexpected"] = True
    else:
        document.pop(mutation)

    with pytest.raises(
        HarnessCompileError, match="resolved execution harness is invalid"
    ):
        compile_harness(synthetic_registry(), document)


@pytest.fixture
def signed_source_bundle(tmp_path: Path):
    bundle = adapter_bundle("custom-adapter")
    store = SourceBundleStore(tmp_path / "source-bundles")
    stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
    issuer = WorkloadObjectReceiptIssuer(Ed25519PrivateKey.generate())
    receipt = issuer.issue_object_receipt(
        object_digest=hashlib.sha256(bundle.archive).hexdigest(),
        size=len(bundle.archive),
    )
    return store, issuer, receipt, stored, bundle


def custom_registry(
    store: SourceBundleStore, issuer: WorkloadObjectReceiptIssuer
) -> HarnessRegistry:
    return HarnessRegistry(
        source_bundle_store=store,
        trusted_signer_keys={issuer.key_id: issuer.public_key_bytes},
    )


class FakeSourceBundleAuthority:
    def __init__(self) -> None:
        self.called = False

    def verify(self, *_args: object) -> object:
        self.called = True
        return object()


class FakeSourceBundleStore:
    def __init__(self) -> None:
        self.called = False

    def get(self, _digest: str) -> object:
        self.called = True
        return object()


class SourceBundleStoreSubclass(SourceBundleStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.called = False

    def get(self, _digest: str) -> object:
        self.called = True
        return object()


def test_registry_rejects_caller_owned_source_bundle_authority() -> None:
    authority = FakeSourceBundleAuthority()

    with pytest.raises(TypeError, match="source_bundle_authority"):
        HarnessRegistry(source_bundle_authority=authority)  # type: ignore[call-arg]

    assert authority.called is False


def test_registry_rejects_fake_and_subclass_source_bundle_stores(
    tmp_path: Path,
) -> None:
    stores = (
        FakeSourceBundleStore(),
        SourceBundleStoreSubclass(tmp_path / "subclass-store"),
    )

    for store in stores:
        with pytest.raises(TypeError, match="exact SourceBundleStore"):
            HarnessRegistry(
                source_bundle_store=store,  # type: ignore[arg-type]
                trusted_signer_keys={},
            )
        assert store.called is False


def test_registry_copies_trusted_signer_key_data_at_construction(
    signed_source_bundle,
) -> None:
    store, issuer, receipt, _stored, bundle = signed_source_bundle
    trusted_keys = {issuer.key_id: issuer.public_key_bytes}
    registry = HarnessRegistry(
        source_bundle_store=store,
        trusted_signer_keys=trusted_keys,
    )
    replacement = WorkloadObjectReceiptIssuer(Ed25519PrivateKey.generate())
    trusted_keys.clear()
    trusted_keys[replacement.key_id] = replacement.public_key_bytes

    identity = _source_identity(bundle)
    registry.register(source_bundle=identity, receipt=receipt)

    assert (
        compile_harness(
            registry, harness_document("custom-adapter", source_bundle=identity)
        ).slug
        == "custom-adapter"
    )


class RecordingEd25519PublicKey(Ed25519PublicKey):
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.public_bytes_raw_called = False

    def __copy__(self):
        return self

    def __eq__(self, other: object) -> bool:
        return self is other

    def public_bytes(self, encoding, format) -> bytes:
        raise AssertionError("public_bytes must not execute")

    def public_bytes_raw(self) -> bytes:
        self.public_bytes_raw_called = True
        return self.raw

    def verify(self, signature: bytes, data: bytes) -> None:
        raise AssertionError("caller-owned verification must not execute")


class BytesSubclass(bytes):
    pass


def test_registry_rejects_an_ed25519_public_key_without_executing_it(
    signed_source_bundle,
) -> None:
    store, issuer, _receipt, _stored, _bundle = signed_source_bundle
    public_key = RecordingEd25519PublicKey(issuer.public_key_bytes)

    with pytest.raises(TypeError, match="trusted signer public key"):
        HarnessRegistry(
            source_bundle_store=store,
            trusted_signer_keys={issuer.key_id: public_key},
        )

    assert public_key.public_bytes_raw_called is False


def test_registry_rejects_non_exact_bytes_trusted_signer_values(
    signed_source_bundle,
) -> None:
    store, issuer, _receipt, _stored, _bundle = signed_source_bundle
    invalid_values = (
        bytearray(issuer.public_key_bytes),
        memoryview(issuer.public_key_bytes),
        issuer.public_key,
        BytesSubclass(issuer.public_key_bytes),
    )

    for value in invalid_values:
        with pytest.raises(TypeError, match="trusted signer public key"):
            HarnessRegistry(
                source_bundle_store=store,
                trusted_signer_keys={issuer.key_id: value},
            )


@pytest.mark.parametrize("length", [0, 31, 33])
def test_registry_rejects_wrong_length_trusted_signer_bytes(
    tmp_path: Path, length: int
) -> None:
    public_key = b"k" * length

    with pytest.raises(TypeError, match="trusted signer public key"):
        HarnessRegistry(
            source_bundle_store=SourceBundleStore(tmp_path / "source-bundles"),
            trusted_signer_keys={hashlib.sha256(public_key).hexdigest(): public_key},
        )


def adapter_document(
    slug: str, *, argv_template: list[str] | None = None, include_inputs: bool = False
) -> dict[str, object]:
    document = {
        "schema_version": 1,
        "slug": slug,
        "contract_version": 1,
        "argv_template": argv_template
        if argv_template is not None
        else ["/opt/vonk/adapters/bin/serve", "--model", "/models"],
        "allowed_parameters": [],
        "allowed_environment": [],
        "mounts": {
            "models": {
                "source": "/run/vonk/models",
                "target": "/models",
                "read_only": True,
            },
            "outputs": {
                "source": "/run/vonk/outputs",
                "target": "/outputs",
                "read_only": False,
                "isolated": True,
            },
        },
        "topology": {"minimum_nodes": 1, "maximum_nodes": 1, "roles": ["entrypoint"]},
        "interfaces": ["openai"],
        "distribution": {
            "platform": "linux/arm64",
            "image": "registry.example/vonk/synthetic@sha256:" + "c" * 64,
            "security": {
                "network_mode": "none",
                "user": "10001:10001",
                "no_new_privileges": True,
                "capabilities": [],
            },
        },
    }
    if include_inputs:
        document["mounts"]["inputs"] = {
            "source": "/run/vonk/inputs",
            "target": "/inputs",
            "read_only": True,
            "isolated": True,
        }
    return document


def adapter_bundle(slug: str, *, document: dict[str, object] | None = None):
    payload = json.dumps(
        document or adapter_document(slug),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return generate_source_bundle({"harness-adapter-v1.json": payload})


def test_reserved_builtin_slug_cannot_be_taken_over_by_custom_registration(
    tmp_path: Path,
) -> None:
    bundle = adapter_bundle("vllm")
    with pytest.raises(HarnessCompileError, match="reserved built-in"):
        issuer = WorkloadObjectReceiptIssuer(Ed25519PrivateKey.generate())
        store = SourceBundleStore(tmp_path / "source-bundles")
        stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
        registry = custom_registry(store, issuer)
        registry.register(
            source_bundle=_source_identity(bundle),
            receipt=issuer.issue_object_receipt(
                object_digest=hashlib.sha256(stored.path.read_bytes()).hexdigest(),
                size=len(bundle.archive),
            ),
        )


def test_mount_validation_rejects_noncanonical_root_and_cross_boundary_paths() -> None:
    projection = compile_harness(synthetic_registry(), harness_document("vllm"))

    for changed in (
        replace(
            projection,
            model_mounts=(replace(projection.model_mounts[0], source="/"),),
        ),
        replace(projection, output_mount=replace(projection.output_mount, source="/")),
        replace(projection, output_mount=replace(projection.output_mount, target="/")),
        replace(
            projection,
            model_mounts=(replace(projection.model_mounts[0], target="/models/"),),
        ),
        replace(
            projection,
            output_mount=replace(projection.output_mount, source="/run//outputs"),
        ),
        replace(
            projection,
            output_mount=replace(
                projection.output_mount, source="/run/vonk/models/out"
            ),
        ),
        replace(
            projection,
            output_mount=replace(
                projection.output_mount, target="/run/vonk/models/out"
            ),
        ),
    ):
        with pytest.raises(HarnessCompileError):
            validate_projection(changed)


class ConcreteBuiltinStub:
    contract_version = 1

    def __init__(self, slug: str) -> None:
        self.slug = slug

    def compile(self, *args, **kwargs):
        return SyntheticHarnessCompiler(self.slug).compile(*args, **kwargs)


class TruthyValue:
    def __bool__(self) -> bool:
        return True


class FalseyValue:
    def __bool__(self) -> bool:
        return False


class EqualToAnyString:
    def __eq__(self, _other: object) -> bool:
        return True


class CallbackString(str):
    def __new__(cls, value: str):
        instance = super().__new__(cls, value)
        instance.influenced_validation = False
        return instance

    def __bool__(self) -> bool:
        self.influenced_validation = True
        return len(self) > 0

    def __eq__(self, other: object) -> bool:
        self.influenced_validation = True
        return str.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        self.influenced_validation = True
        return str.__ne__(self, other)

    def __hash__(self) -> int:
        self.influenced_validation = True
        return str.__hash__(self)

    def lower(self) -> str:
        self.influenced_validation = True
        return str.lower(self)

    def startswith(self, prefix, start=0, end=None) -> bool:
        self.influenced_validation = True
        if end is None:
            return str.startswith(self, prefix, start)
        return str.startswith(self, prefix, start, end)


def test_trusted_builtin_composition_accepts_concrete_compilers_for_every_slug() -> (
    None
):
    registry = HarnessRegistry.from_trusted_builtins(
        TrustedBuiltinComposition(
            tuple(ConcreteBuiltinStub(slug) for slug in BUILTIN_HARNESS_SLUGS)
        )
    )

    for slug in BUILTIN_HARNESS_SLUGS:
        assert compile_harness(registry, harness_document(slug)).slug == slug


@pytest.mark.parametrize(
    "compilers",
    [
        tuple(ConcreteBuiltinStub("wrong-slug") for _ in BUILTIN_HARNESS_SLUGS),
        tuple(ConcreteBuiltinStub("vllm") for _ in BUILTIN_HARNESS_SLUGS),
    ],
)
def test_trusted_builtin_composition_rejects_wrong_or_duplicate_slugs(
    compilers,
) -> None:
    with pytest.raises(HarnessCompileError):
        HarnessRegistry.from_trusted_builtins(TrustedBuiltinComposition(compilers))


def test_trusted_builtin_composition_rejects_wrong_contract_version() -> None:
    compiler = ConcreteBuiltinStub("vllm")
    compiler.contract_version = 2
    compilers = (compiler,) + tuple(
        ConcreteBuiltinStub(slug) for slug in BUILTIN_HARNESS_SLUGS[1:]
    )

    with pytest.raises(HarnessCompileError, match="identity"):
        HarnessRegistry.from_trusted_builtins(TrustedBuiltinComposition(compilers))


def test_trusted_builtin_slug_rejects_a_string_subclass_before_using_it() -> None:
    slug = CallbackString("vllm")
    compilers = (ConcreteBuiltinStub(slug),) + tuple(
        ConcreteBuiltinStub(value) for value in BUILTIN_HARNESS_SLUGS[1:]
    )

    with pytest.raises(HarnessCompileError, match="identity"):
        HarnessRegistry.from_trusted_builtins(TrustedBuiltinComposition(compilers))

    assert slug.influenced_validation is False


@pytest.mark.parametrize("version", [True, False, 1.0, "1"])
def test_trusted_builtin_composition_rejects_non_exact_contract_version(
    version: object,
) -> None:
    compiler = ConcreteBuiltinStub("vllm")
    compiler.contract_version = version
    compilers = (compiler,) + tuple(
        ConcreteBuiltinStub(slug) for slug in BUILTIN_HARNESS_SLUGS[1:]
    )

    with pytest.raises(HarnessCompileError, match="identity"):
        HarnessRegistry.from_trusted_builtins(TrustedBuiltinComposition(compilers))


@pytest.mark.parametrize("version", [True, False, 1.0, "1"])
def test_builtin_compile_rechecks_exact_contract_version(version: object) -> None:
    compilers = tuple(ConcreteBuiltinStub(slug) for slug in BUILTIN_HARNESS_SLUGS)
    registry = HarnessRegistry.from_trusted_builtins(
        TrustedBuiltinComposition(compilers)
    )
    compilers[0].contract_version = version

    with pytest.raises(HarnessCompileError, match="identity"):
        compile_harness(registry, harness_document("vllm"))


def _source_identity(bundle) -> dict[str, object]:
    return {
        "sha256": bundle.sha256,
        "expected_bytes": len(bundle.archive),
        "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
    }


def test_custom_adapter_requires_a_real_signed_source_bundle(
    signed_source_bundle,
) -> None:
    store, issuer, receipt, _stored, bundle = signed_source_bundle
    registry = custom_registry(store, issuer)
    tampered = replace(receipt, signature=replace(receipt.signature, value="0" * 128))

    with pytest.raises(HarnessCompileError, match="source bundle receipt"):
        registry.register(
            source_bundle=_source_identity(bundle),
            receipt=tampered,
        )


@pytest.mark.parametrize(
    ("object_digest", "size"),
    [("f" * 64, None), (None, 1)],
)
def test_custom_adapter_rejects_a_receipt_for_different_bytes(
    signed_source_bundle, object_digest: str | None, size: int | None
) -> None:
    store, issuer, _receipt, _stored, bundle = signed_source_bundle
    registry = custom_registry(store, issuer)
    wrong_receipt = issuer.issue_object_receipt(
        object_digest=object_digest or hashlib.sha256(bundle.archive).hexdigest(),
        size=size or len(bundle.archive),
    )

    with pytest.raises(HarnessCompileError, match="bind exact archive"):
        registry.register(
            source_bundle=_source_identity(bundle),
            receipt=wrong_receipt,
        )


def test_custom_adapter_rejects_wrong_signer_and_caller_loader(
    signed_source_bundle,
) -> None:
    store, issuer, receipt, _stored, bundle = signed_source_bundle
    other = WorkloadObjectReceiptIssuer(Ed25519PrivateKey.generate())
    wrong_signer_receipt = other.issue_object_receipt(
        object_digest=hashlib.sha256(bundle.archive).hexdigest(),
        size=len(bundle.archive),
    )
    registry = custom_registry(store, issuer)
    identity = _source_identity(bundle)
    with pytest.raises(HarnessCompileError, match="source bundle receipt"):
        registry.register(
            source_bundle=identity,
            receipt=wrong_signer_receipt,
        )

    with pytest.raises(TypeError):
        registry.register(
            SyntheticHarnessCompiler("custom-adapter"),
            source_bundle=identity,
            receipt=receipt,
        )


def test_custom_adapter_rechecks_verified_bytes_at_compile_time(
    signed_source_bundle,
) -> None:
    store, issuer, receipt, stored, bundle = signed_source_bundle
    registry = custom_registry(store, issuer)
    identity = _source_identity(bundle)
    registry.register(
        source_bundle=identity,
        receipt=receipt,
    )
    stored.path.write_bytes(b"changed")

    with pytest.raises(HarnessCompileError, match="source bundle verification"):
        compile_harness(
            registry, harness_document("custom-adapter", source_bundle=identity)
        )


def test_custom_adapter_rejects_changed_raw_archive_with_same_manifest_and_length(
    signed_source_bundle,
) -> None:
    store, issuer, receipt, stored, bundle = signed_source_bundle
    registry = custom_registry(store, issuer)
    identity = _source_identity(bundle)
    registry.register(
        source_bundle=identity,
        receipt=receipt,
    )
    changed = bytearray(bundle.archive)
    changed[-1] = 1
    stored.path.write_bytes(changed)

    with pytest.raises(HarnessCompileError, match="receipt"):
        compile_harness(
            registry, harness_document("custom-adapter", source_bundle=identity)
        )


def test_custom_adapter_rejects_adapter_document_mutation(
    signed_source_bundle,
) -> None:
    store, issuer, receipt, stored, bundle = signed_source_bundle
    registry = custom_registry(store, issuer)
    identity = _source_identity(bundle)
    registry.register(source_bundle=identity, receipt=receipt)
    stored.path.write_bytes(
        adapter_bundle(
            "custom-adapter",
            document=adapter_document(
                "custom-adapter", argv_template=["/opt/vonk/adapters/bin/changed"]
            ),
        ).archive
    )

    with pytest.raises(HarnessCompileError, match="source bundle"):
        compile_harness(
            registry, harness_document("custom-adapter", source_bundle=identity)
        )


def test_custom_adapter_rejects_duplicate_registration(signed_source_bundle) -> None:
    store, issuer, receipt, _stored, bundle = signed_source_bundle
    registry = custom_registry(store, issuer)
    identity = _source_identity(bundle)
    registry.register(
        source_bundle=identity,
        receipt=receipt,
    )

    with pytest.raises(HarnessCompileError, match="already registered"):
        registry.register(
            source_bundle=identity,
            receipt=receipt,
        )


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"adapter.py": b"pass\n"}, "adapter document"),
        (
            {"harness-adapter-v1.json": b'{"schema_version":1,"schema_version":1}'},
            "adapter document",
        ),
        (
            {"harness-adapter-v1.json": b'{ "schema_version": 1 }'},
            "adapter document",
        ),
    ],
)
def test_custom_adapter_rejects_missing_duplicate_or_noncanonical_document(
    tmp_path: Path, files: dict[str, bytes], message: str
) -> None:
    bundle = generate_source_bundle(files)
    store = SourceBundleStore(tmp_path / "source-bundles")
    stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
    issuer = WorkloadObjectReceiptIssuer(Ed25519PrivateKey.generate())
    registry = custom_registry(store, issuer)
    receipt = issuer.issue_object_receipt(
        object_digest=hashlib.sha256(stored.path.read_bytes()).hexdigest(),
        size=len(bundle.archive),
    )

    with pytest.raises(HarnessCompileError, match=message):
        registry.register(source_bundle=_source_identity(bundle), receipt=receipt)


def test_custom_adapter_output_is_determined_by_its_signed_document(
    tmp_path: Path,
) -> None:
    first = adapter_bundle("custom-adapter")
    second = adapter_bundle(
        "custom-adapter",
        document=adapter_document(
            "custom-adapter", argv_template=["/opt/vonk/adapters/bin/alternate"]
        ),
    )
    issuer = WorkloadObjectReceiptIssuer(Ed25519PrivateKey.generate())

    def register(bundle):
        store = SourceBundleStore(tmp_path / bundle.sha256)
        stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
        registry = custom_registry(store, issuer)
        registry.register(
            source_bundle=_source_identity(bundle),
            receipt=issuer.issue_object_receipt(
                object_digest=hashlib.sha256(stored.path.read_bytes()).hexdigest(),
                size=len(bundle.archive),
            ),
        )
        return registry

    first_projection = compile_harness(
        register(first),
        harness_document("custom-adapter", source_bundle=_source_identity(first)),
    )
    second_projection = compile_harness(
        register(second),
        harness_document("custom-adapter", source_bundle=_source_identity(second)),
    )

    assert first_projection.command == (
        "/opt/vonk/adapters/bin/serve",
        "--model",
        "/models",
    )
    assert second_projection.command == ("/opt/vonk/adapters/bin/alternate",)


def test_custom_adapter_can_bind_a_signed_read_only_input_mount(
    tmp_path: Path,
) -> None:
    document = adapter_document("custom-input", include_inputs=True)
    document["interfaces"] = ["artifact-job"]
    bundle = adapter_bundle("custom-input", document=document)
    store = SourceBundleStore(tmp_path / "source-bundles")
    stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
    issuer = WorkloadObjectReceiptIssuer(Ed25519PrivateKey.generate())
    registry = custom_registry(store, issuer)
    identity = _source_identity(bundle)
    registry.register(
        source_bundle=identity,
        receipt=issuer.issue_object_receipt(
            object_digest=hashlib.sha256(stored.path.read_bytes()).hexdigest(),
            size=len(bundle.archive),
        ),
    )
    harness = harness_document("custom-input", source_bundle=identity)
    harness["adapters"] = ["artifact-job"]
    distribution = distribution_document("custom-input")
    distribution["implements_harness"]["content_sha256"] = catalog_content_sha256(
        harness
    )
    recipe = recipe_document(harness, distribution)
    recipe["interfaces"] = [
        {
            "adapter": "artifact-job",
            "path": "/outputs",
            "input": {
                "path": "/inputs",
                "required": True,
                "media_types": ["image/png"],
                "max_bytes": 1024,
            },
        }
    ]

    projection = registry.compile(
        harness,
        recipe=recipe,
        distribution=distribution,
        patch=None,
        parameters={},
        topology=recipe["topology"],
        role="entrypoint",
        rank=0,
    )

    assert projection.input_mount is not None
    assert projection.input_mount.target == "/inputs"
    assert projection.input_mount.read_only is True
    assert projection.input_mount.isolated is True


@pytest.mark.parametrize(
    "argv_template",
    [
        ["/usr/bin/bash", "-c", "id"],
        ["/bin/dash", "script"],
        ["/usr/bin/env", "bash"],
        ["/bin/busybox", "sh"],
        ["/usr/local/bin/start-adapter.sh"],
        ["/opt/vonk/adapters/bin/bash", "script"],
        ["/opt/vonk/adapters/bin/env", "bash"],
        ["/opt/vonk/adapters/bin/../serve"],
        ["/opt/vonk/adapters/bin/nested/serve"],
        ["/opt/vonk/adapters/bin/serve", "model;id"],
    ],
)
def test_custom_adapter_rejects_non_allowlisted_executables_and_shell_launchers(
    tmp_path: Path, argv_template: list[str]
) -> None:
    document = adapter_document("custom-adapter", argv_template=argv_template)
    bundle = adapter_bundle("custom-adapter", document=document)
    store = SourceBundleStore(tmp_path / "source-bundles")
    stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
    issuer = WorkloadObjectReceiptIssuer(Ed25519PrivateKey.generate())
    registry = custom_registry(store, issuer)

    with pytest.raises(HarnessCompileError, match="adapter"):
        registry.register(
            source_bundle=_source_identity(bundle),
            receipt=issuer.issue_object_receipt(
                object_digest=hashlib.sha256(stored.path.read_bytes()).hexdigest(),
                size=len(bundle.archive),
            ),
        )


def test_custom_adapter_rejects_unknown_document_fields(tmp_path: Path) -> None:
    document = {**adapter_document("custom-adapter"), "unexpected": True}
    bundle = adapter_bundle("custom-adapter", document=document)
    store = SourceBundleStore(tmp_path / "source-bundles")
    stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
    issuer = WorkloadObjectReceiptIssuer(Ed25519PrivateKey.generate())
    registry = custom_registry(store, issuer)

    with pytest.raises(HarnessCompileError, match="adapter"):
        registry.register(
            source_bundle=_source_identity(bundle),
            receipt=issuer.issue_object_receipt(
                object_digest=hashlib.sha256(stored.path.read_bytes()).hexdigest(),
                size=len(bundle.archive),
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("no_new_privileges", 1),
        ("no_new_privileges", 0),
        ("model_read_only", 1),
        ("model_read_only", 0),
        ("output_read_only", 1),
        ("output_read_only", 0),
        ("output_isolated", 1),
        ("output_isolated", 0),
        ("no_new_privileges", TruthyValue()),
        ("model_read_only", TruthyValue()),
        ("output_read_only", TruthyValue()),
        ("output_isolated", TruthyValue()),
    ],
)
def test_projection_rejects_non_boolean_security_and_mount_values(
    field: str, value: object
) -> None:
    projection = compile_harness(synthetic_registry(), harness_document("vllm"))
    if field == "no_new_privileges":
        changed = replace(projection, no_new_privileges=value)
    elif field == "model_read_only":
        changed = replace(
            projection,
            model_mounts=(replace(projection.model_mounts[0], read_only=value),),
        )
    elif field == "output_read_only":
        changed = replace(
            projection, output_mount=replace(projection.output_mount, read_only=value)
        )
    else:
        changed = replace(
            projection, output_mount=replace(projection.output_mount, isolated=value)
        )

    with pytest.raises(HarnessCompileError):
        validate_projection(changed)


@pytest.mark.parametrize(
    "capabilities",
    [False, 0, None, [], TruthyValue(), FalseyValue()],
)
def test_projection_requires_exact_empty_capabilities_tuple(
    capabilities: object,
) -> None:
    projection = compile_harness(synthetic_registry(), harness_document("vllm"))

    with pytest.raises(HarnessCompileError, match="capabilities"):
        validate_projection(replace(projection, capabilities=capabilities))


@pytest.mark.parametrize("parser", [structured_command, custom_adapter_command])
def test_command_parsers_reject_a_string_subclass_before_using_it(parser) -> None:
    argument = CallbackString("--model")

    with pytest.raises(HarnessCompileError, match="command"):
        parser(("/opt/vonk/adapters/bin/serve", argument))

    assert argument.influenced_validation is False


def test_projection_command_rejects_a_string_subclass_before_using_it() -> None:
    projection = compile_harness(synthetic_registry(), harness_document("vllm"))
    argument = CallbackString("--model")

    with pytest.raises(HarnessCompileError, match="command"):
        validate_projection(
            replace(
                projection,
                command=("/opt/vonk/adapters/bin/serve", argument),
            )
        )

    assert argument.influenced_validation is False


def test_registry_role_rejects_a_string_subclass_before_using_it() -> None:
    document = harness_document("vllm")
    distribution = distribution_document("vllm")
    implementation = distribution["implements_harness"]
    assert isinstance(implementation, dict)
    implementation["content_sha256"] = catalog_content_sha256(document)
    role = CallbackString("entrypoint")
    recipe = recipe_document(document, distribution)

    with pytest.raises(HarnessCompileError, match="topology binding"):
        synthetic_registry().compile(
            document,
            recipe=recipe,
            distribution=distribution,
            patch=None,
            parameters={},
            topology=recipe["topology"],
            role=role,
            rank=0,
        )

    assert role.influenced_validation is False


@pytest.mark.parametrize(
    "change",
    [
        lambda projection: replace(projection, command=list(projection.command)),
        lambda projection: replace(
            projection, model_mounts=list(projection.model_mounts)
        ),
        lambda projection: replace(projection, network_mode=EqualToAnyString()),
        lambda projection: replace(projection, architecture=EqualToAnyString()),
    ],
)
def test_projection_rejects_wrong_security_contract_types(change) -> None:
    projection = compile_harness(synthetic_registry(), harness_document("vllm"))

    with pytest.raises(HarnessCompileError):
        validate_projection(change(projection))


@pytest.mark.parametrize("version", [True, False, 1.0, "1"])
def test_custom_adapter_rejects_non_exact_contract_version(
    tmp_path: Path, version: object
) -> None:
    bundle = adapter_bundle(
        "custom-adapter",
        document={**adapter_document("custom-adapter"), "contract_version": version},
    )
    store = SourceBundleStore(tmp_path / "source-bundles")
    stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
    issuer = WorkloadObjectReceiptIssuer(Ed25519PrivateKey.generate())
    registry = custom_registry(store, issuer)
    receipt = issuer.issue_object_receipt(
        object_digest=hashlib.sha256(stored.path.read_bytes()).hexdigest(),
        size=len(bundle.archive),
    )

    with pytest.raises(HarnessCompileError, match="adapter"):
        registry.register(source_bundle=_source_identity(bundle), receipt=receipt)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda projection: replace(
                projection, image="registry.example/runtime:latest"
            ),
            "digest-pinned",
        ),
        (
            lambda projection: replace(projection, network_mode="public"),
            "offline network",
        ),
        (lambda projection: replace(projection, user="0:0"), "numeric and non-root"),
        (
            lambda projection: replace(projection, no_new_privileges=False),
            "no-new-privileges",
        ),
        (
            lambda projection: replace(projection, capabilities=("NET_ADMIN",)),
            "capabilities",
        ),
        (
            lambda projection: replace(
                projection,
                model_mounts=(replace(projection.model_mounts[0], read_only=False),),
            ),
            "model mounts",
        ),
        (
            lambda projection: replace(
                projection,
                model_mounts=(
                    replace(projection.model_mounts[0], source="/var/run/docker.sock"),
                ),
            ),
            "socket",
        ),
        (
            lambda projection: replace(
                projection,
                output_mount=replace(
                    projection.output_mount, target="/var/run/podman.sock"
                ),
            ),
            "socket",
        ),
        (
            lambda projection: replace(
                projection,
                output_mount=replace(projection.output_mount, target="/models/outputs"),
            ),
            "overlap",
        ),
        (
            lambda projection: replace(
                projection,
                output_mount=replace(projection.output_mount, target="outputs"),
            ),
            "absolute",
        ),
        (
            lambda projection: replace(
                projection,
                output_mount=replace(
                    projection.output_mount, target="/models/../outputs"
                ),
            ),
            "escaping",
        ),
    ],
)
def test_projection_rejects_runtime_security_bypasses(change, message: str) -> None:
    projection = compile_harness(synthetic_registry(), harness_document("vllm"))

    with pytest.raises(HarnessCompileError, match=message):
        validate_projection(change(projection))

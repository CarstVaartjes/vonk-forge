from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from vonk_control.harnesses import BUILTIN_HARNESS_SLUGS, HarnessCompileError
from vonk_control.harnesses.common import SyntheticHarnessCompiler, validate_projection
from vonk_control.harnesses.registry import (
    HarnessRegistry,
    SourceBundleAuthority,
    SourceBundleReceiptVerifier,
)
from vonk_control.package_helper_authority import PackageObjectReceiptIssuer
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle


def harness_document(
    slug: str, *, source_bundle: dict[str, object] | None = None
) -> dict[str, object]:
    return {
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
        "source_bundle": source_bundle
        or {
            "sha256": "a" * 64,
            "expected_bytes": 2048,
            "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
        },
    }


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
        "sha256": "d" * 64,
    }


def compile_harness(registry: HarnessRegistry, document: dict[str, object]):
    identity = document["identity"]
    assert isinstance(identity, dict)
    return registry.compile(
        document,
        recipe={"runtime": {"entrypoint": ["serve", "--model", "/models"]}},
        distribution=distribution_document(str(identity["slug"])),
        patch=None,
        parameters={},
        topology={"node_count": 1},
        role="entrypoint",
        rank=0,
    )


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_registry_compiles_each_exact_builtin_harness_slug(slug: str) -> None:
    projection = compile_harness(
        HarnessRegistry.with_builtins(), harness_document(slug)
    )

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
        compile_harness(
            HarnessRegistry.with_builtins(), harness_document("unknown-harness")
        )


def test_registry_rejects_a_distribution_for_a_different_harness() -> None:
    with pytest.raises(HarnessCompileError, match="does not implement harness"):
        HarnessRegistry.with_builtins().compile(
            harness_document("sglang"),
            recipe={"runtime": {"entrypoint": ["serve"]}},
            distribution=distribution_document("vllm"),
            patch=None,
            parameters={},
            topology={"node_count": 1},
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
        compile_harness(HarnessRegistry.with_builtins(), document)


@pytest.fixture
def signed_source_authority(tmp_path: Path):
    bundle = generate_source_bundle({"adapter.py": b"print('adapter')\n"})
    store = SourceBundleStore(tmp_path / "source-bundles")
    stored = store.put(bundle.sha256, io.BytesIO(bundle.archive))
    issuer = PackageObjectReceiptIssuer(Ed25519PrivateKey.generate())
    verifier = SourceBundleReceiptVerifier(issuer.public_key)
    authority = SourceBundleAuthority(store, verifier)
    receipt = issuer.issue_object_receipt(
        object_digest=bundle.sha256, size=len(bundle.archive)
    )
    return authority, issuer, receipt, stored, bundle


def test_reserved_builtin_slug_cannot_be_taken_over_by_custom_registration() -> None:
    with pytest.raises(HarnessCompileError, match="reserved built-in"):
        HarnessRegistry().register(SyntheticHarnessCompiler("vllm"))


def test_custom_adapter_requires_a_real_signed_source_bundle(
    signed_source_authority,
) -> None:
    authority, issuer, receipt, _stored, bundle = signed_source_authority
    registry = HarnessRegistry(source_bundle_authority=authority)
    compiler = SyntheticHarnessCompiler(
        "custom-adapter",
        source_bundle_digest=bundle.sha256,
        source_bundle_signer=issuer.key_id,
    )
    tampered = replace(receipt, signature=replace(receipt.signature, value="0" * 128))

    with pytest.raises(HarnessCompileError, match="source bundle receipt"):
        registry.register(
            compiler,
            source_bundle={
                "sha256": bundle.sha256,
                "expected_bytes": len(bundle.archive),
                "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
            },
            receipt=tampered,
        )


def test_custom_adapter_rejects_wrong_signer_and_compiler_binding(
    signed_source_authority,
) -> None:
    authority, issuer, _receipt, _stored, bundle = signed_source_authority
    other = PackageObjectReceiptIssuer(Ed25519PrivateKey.generate())
    wrong_signer_receipt = other.issue_object_receipt(
        object_digest=bundle.sha256, size=len(bundle.archive)
    )
    registry = HarnessRegistry(source_bundle_authority=authority)
    compiler = SyntheticHarnessCompiler(
        "custom-adapter",
        source_bundle_digest="f" * 64,
        source_bundle_signer=issuer.key_id,
    )
    identity = {
        "sha256": bundle.sha256,
        "expected_bytes": len(bundle.archive),
        "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
    }
    with pytest.raises(HarnessCompileError, match="source bundle receipt"):
        registry.register(
            compiler, source_bundle=identity, receipt=wrong_signer_receipt
        )

    receipt = issuer.issue_object_receipt(
        object_digest=bundle.sha256, size=len(bundle.archive)
    )
    with pytest.raises(HarnessCompileError, match="compiler source bundle identity"):
        registry.register(compiler, source_bundle=identity, receipt=receipt)


def test_custom_adapter_rechecks_verified_bytes_at_compile_time(
    signed_source_authority,
) -> None:
    authority, issuer, receipt, stored, bundle = signed_source_authority
    registry = HarnessRegistry(source_bundle_authority=authority)
    compiler = SyntheticHarnessCompiler(
        "custom-adapter",
        source_bundle_digest=bundle.sha256,
        source_bundle_signer=issuer.key_id,
    )
    identity = {
        "sha256": bundle.sha256,
        "expected_bytes": len(bundle.archive),
        "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
    }
    registry.register(compiler, source_bundle=identity, receipt=receipt)
    stored.path.write_bytes(b"changed")

    with pytest.raises(HarnessCompileError, match="source bundle verification"):
        compile_harness(
            registry, harness_document("custom-adapter", source_bundle=identity)
        )


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
    projection = compile_harness(
        HarnessRegistry.with_builtins(), harness_document("vllm")
    )

    with pytest.raises(HarnessCompileError, match=message):
        validate_projection(change(projection))

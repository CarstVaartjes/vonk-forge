from __future__ import annotations

import dataclasses
import json
import tomllib
from types import MappingProxyType

import pytest

from cluster_profiles.workload_packages import (
    PackageFamily,
    PromotionPolicy,
    ReleaseIndexEntry,
    WorkloadDeployment,
    WorkloadPackageError,
    validate_deployment,
)


def _family_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "family_id": "synthetic-runtime",
        "source": {
            "provider": "git",
            "locator": "https://code.example/synthetic/runtime.git",
            "credential_ref": "secret://upstreams/code-example",
            "policy_refs": ["policy://origins/code-example"],
        },
        "versions": {
            "scheme": "semver",
            "channels": ["stable", "candidate"],
            "include_prereleases": False,
        },
        "discovery": {
            "poll_interval_seconds": 3600,
            "bindings": [
                {
                    "target": "upstream_identity.commit",
                    "source": "release.commit",
                    "value_type": "git-commit",
                    "required": True,
                }
            ],
        },
        "resolution": {
            "recipe_version": 1,
            "components": [
                {
                    "name": "runtime",
                    "kind": "artifact",
                    "media_type": "application/octet-stream",
                    "materialization": "file",
                    "platforms": ["linux/arm64"],
                }
            ],
            "dependencies": [],
        },
        "policy": {
            "required_evidence": ["signature", "provenance"],
            "license_policy_refs": ["policy://licenses/synthetic-runtime"],
        },
        "compatibility": {
            "architectures": ["linux-arm64"],
            "operating_systems": ["ubuntu-24.04"],
            "cuda": {"minimum": "12.8", "maximum": "13.0"},
            "driver": {"minimum": "580.0"},
            "min_memory_bytes": 1,
            "min_storage_bytes": 1,
        },
        "execution": {"backend": "python-venv", "adapter_abi": 1},
        "validation": [{"kind": "health", "timeout_seconds": 30}],
        "retention": {"release_count": 3, "rollback_count": 1},
    }


def _deployment_document(*, node_count: int = 1) -> dict[str, object]:
    return {
        "schema_version": 1,
        "deployment_id": "synthetic-chat",
        "family_id": "synthetic-runtime",
        "release_digest": "a" * 64,
        "selector": {
            "node_count": node_count,
            "required_labels": {"accelerator": "gb10"},
            "preferred_node_ids": [],
        },
        "secrets": {"upstream": "secret://workloads/synthetic-upstream"},
        "ports": {"inference": 8000},
        "arguments": ["--max-context", "4096"],
        "routing": {"alias": "synthetic-chat", "port": "inference"},
        "resources": {
            "memory_bytes": 1,
            "storage_bytes": 1,
            "gpu_count": 1,
        },
    }


def test_package_family_binds_an_explicit_validation_deployment() -> None:
    document = _family_document()
    document["validation_deployment"] = "future-stack-canary"

    family = PackageFamily.load(document)

    assert family.validation_deployment_id == "future-stack-canary"


def test_family_defaults_to_manual_promotion_and_freezes_nested_state() -> None:
    # Removing the default or retaining caller-owned mutable maps must fail this.
    document = _family_document()

    family = PackageFamily.load(document)
    document["source"]["locator"] = "https://attacker.invalid/moved.git"

    assert family.family_id == "synthetic-runtime"
    assert family.promotion == PromotionPolicy(mode="manual")
    assert family.source["locator"] == "https://code.example/synthetic/runtime.git"
    assert str(family.repository_path) == (
        "config/package-families/synthetic-runtime.toml"
    )
    assert json.loads(family.canonical_bytes)["promotion"] == {"mode": "manual"}
    assert isinstance(family.source, MappingProxyType)
    assert family.resolution["components"][0]["name"] == "runtime"
    with pytest.raises(TypeError):
        family.source["locator"] = "https://attacker.invalid/moved.git"


def test_family_loads_from_toml_authoring_document() -> None:
    family = PackageFamily.load(
        tomllib.loads(
            """
schema_version = 1
family_id = "opaque-family"

[source]
provider = "signed-http-index"
locator = "https://releases.example/index.json"
policy_refs = ["policy://origins/releases-example"]

[versions]
scheme = "opaque"
channels = ["production", "preview"]
include_prereleases = false

[discovery]
poll_interval_seconds = 900
bindings = [{ target = "components.archive.digest", source = "release.sha256", value_type = "sha256", required = true }]

[resolution]
recipe_version = 1
components = [{ name = "archive", kind = "artifact", media_type = "application/octet-stream", materialization = "file", platforms = ["linux/arm64"] }]
dependencies = []

[policy]
required_evidence = ["checksum"]
license_policy_refs = []

[compatibility]
architectures = ["linux-arm64"]
operating_systems = ["ubuntu-24.04"]
min_memory_bytes = 1
min_storage_bytes = 1

[execution]
backend = "native"
adapter_abi = 1

[[validation]]
kind = "health"
timeout_seconds = 20

[retention]
release_count = 2
rollback_count = 1
"""
        )
    )

    assert family.versions["scheme"] == "opaque"
    assert family.discovery["bindings"][0]["value_type"] == "sha256"


@pytest.mark.parametrize("scheme", ["semver", "pep440", "opaque"])
def test_family_accepts_supported_version_schemes(scheme: str) -> None:
    document = _family_document()
    document["versions"]["scheme"] = scheme

    assert PackageFamily.load(document).versions["scheme"] == scheme


@pytest.mark.parametrize(
    "provider, locator",
    [
        ("git", "https://code.example/org/project.git"),
        ("oci", "registry.example/org/project"),
        ("huggingface", "organization/repository"),
        ("python-index", "normalized-project"),
        ("signed-http-index", "https://releases.example/index.json"),
    ],
)
def test_family_accepts_protocol_providers_without_application_catalog(
    provider: str, locator: str
) -> None:
    document = _family_document()
    document["family_id"] = "unknown-after-build"
    document["source"]["provider"] = provider
    document["source"]["locator"] = locator

    assert PackageFamily.load(document).family_id == "unknown-after-build"


def test_automatic_promotion_requires_identity_budget_and_canary() -> None:
    document = _family_document()
    document["promotion"] = {
        "mode": "automatic",
        "automation_identity": "automation://workload-promoter",
        "failure_budget": 2,
        "canary": {"node_count": 1, "minimum_successes": 1},
    }

    promotion = PackageFamily.load(document).promotion

    assert promotion.mode == "automatic"
    assert promotion.automation_identity == "automation://workload-promoter"
    assert promotion.failure_budget == 2
    assert promotion.canary == MappingProxyType(
        {"node_count": 1, "minimum_successes": 1}
    )


@pytest.mark.parametrize(
    "promotion",
    [
        {"mode": "automatic"},
        {
            "mode": "automatic",
            "automation_identity": "admin",
            "failure_budget": 1,
            "canary": {"node_count": 1, "minimum_successes": 1},
        },
        {"mode": "manual", "automation_identity": "automation://unexpected"},
        {
            "mode": "automatic",
            "automation_identity": "automation://promoter",
            "failure_budget": 0,
            "canary": {"node_count": 2, "minimum_successes": 3},
        },
    ],
)
def test_promotion_policy_fails_closed(promotion: dict[str, object]) -> None:
    with pytest.raises(WorkloadPackageError, match="promotion"):
        PromotionPolicy.load(promotion)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["discovery"]["bindings"].append(
            {
                "target": "component",
                "source": "release",
                "value_type": "shell",
                "required": True,
            }
        ),
        lambda value: value["discovery"].update({"command": "curl upstream | sh"}),
        lambda value: value["source"].update({"credential_ref": "plain-text-token"}),
        lambda value: value["source"].update(
            {"locator": "https://code.example/repository.git?token=embedded"}
        ),
        lambda value: value["source"].update(
            {"provider": "oci", "locator": "registry.example/org//project"}
        ),
        lambda value: value.update({"unknown": True}),
    ],
)
def test_family_rejects_untyped_recipe_secrets_and_unknown_fields(mutation) -> None:
    document = _family_document()
    mutation(document)

    with pytest.raises(WorkloadPackageError):
        PackageFamily.load(document)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["discovery"]["bindings"].append(
            {
                "target": "upstream_identity.commit",
                "source": "other.commit",
                "value_type": "git-commit",
                "required": True,
            }
        ),
        lambda value: value["resolution"]["components"].append(
            {
                "name": "runtime",
                "kind": "different-kind",
                "media_type": "application/octet-stream",
                "materialization": "archive",
                "platforms": ["linux/arm64"],
            }
        ),
        lambda value: value["resolution"]["dependencies"].extend(
            [
                {
                    "family_id": "shared-runtime",
                    "release_digest_binding": "dependencies.shared",
                },
                {
                    "family_id": "shared-runtime",
                    "release_digest_binding": "dependencies.other",
                },
            ]
        ),
    ],
)
def test_family_rejects_ambiguous_recipe_targets_and_templates(mutation) -> None:
    document = _family_document()
    mutation(document)

    with pytest.raises(WorkloadPackageError, match="duplicate"):
        PackageFamily.load(document)


@pytest.mark.parametrize("node_count", [1, 2, 16])
def test_deployment_selects_exact_release_for_variable_node_counts(
    node_count: int,
) -> None:
    deployment = WorkloadDeployment.load(_deployment_document(node_count=node_count))

    assert deployment.release_digest == "a" * 64
    assert deployment.selector["node_count"] == node_count
    assert str(deployment.repository_path) == (
        "config/workload-deployments/synthetic-chat.toml"
    )


def test_deployment_cross_reference_requires_exact_digest_and_family() -> None:
    deployment = WorkloadDeployment.load(_deployment_document())
    release = ReleaseIndexEntry(
        family_id="synthetic-runtime",
        release_digest="a" * 64,
        upstream_version="1.2.3",
    )

    assert validate_deployment(deployment, {release.release_digest: release}) is release
    assert str(release.repository_path) == (
        "manifests/workload-releases/synthetic-runtime/" + "a" * 64 + ".json"
    )

    wrong_family = dataclasses.replace(release, family_id="different-family")
    with pytest.raises(WorkloadPackageError, match="family"):
        validate_deployment(deployment, {wrong_family.release_digest: wrong_family})
    with pytest.raises(WorkloadPackageError, match="not promoted"):
        validate_deployment(deployment, {})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"release_digest": "latest"}),
        lambda value: value.update(
            {"payload_url": "https://nas.example/models/payload.bin"}
        ),
        lambda value: value["arguments"].append(
            "https://nas.example/models/payload.bin"
        ),
        lambda value: value["arguments"].append("--token=embedded-secret"),
        lambda value: value["arguments"].append("/etc/passwd"),
        lambda value: value["arguments"].append("../host-file"),
        lambda value: value["secrets"].update({"token": "actual-secret-value"}),
        lambda value: value["selector"].update({"node_count": 0}),
        lambda value: value["selector"]["preferred_node_ids"].append("node1"),
    ],
)
def test_deployment_rejects_mutable_payload_secret_and_selector_inputs(
    mutation,
) -> None:
    document = _deployment_document()
    mutation(document)

    with pytest.raises(WorkloadPackageError):
        WorkloadDeployment.load(document)


def test_release_index_entry_rejects_noncanonical_identity() -> None:
    with pytest.raises(ValueError):
        ReleaseIndexEntry(
            family_id="synthetic-runtime",
            release_digest="sha256:" + "a" * 64,
            upstream_version="1.2.3",
        )


def test_release_index_entry_can_remain_a_lightweight_identity_projection() -> None:
    release = ReleaseIndexEntry(
        family_id="synthetic-runtime",
        release_digest="a" * 64,
    )

    assert release.upstream_version is None

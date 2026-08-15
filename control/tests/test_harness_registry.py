from __future__ import annotations

from dataclasses import replace

import pytest

from vonk_control.harnesses import BUILTIN_HARNESS_SLUGS, HarnessCompileError
from vonk_control.harnesses.common import SyntheticHarnessCompiler
from vonk_control.harnesses.registry import HarnessRegistry


def harness_document(slug: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "execution-harness",
        "identity": {"publisher": "vonk-forge", "slug": slug},
        "runtime_interface": "vonk.runtime.v1",
        "source_bundle": {
            "sha256": "a" * 64,
            "expected_bytes": 2048,
            "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
        },
    }


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_registry_compiles_each_exact_builtin_harness_slug(slug: str) -> None:
    projection = HarnessRegistry.with_builtins().compile(
        harness_document(slug),
        recipe={"runtime": {"entrypoint": ["serve", "--model", "/models"]}},
        distribution={"platform": "linux/arm64"},
        patch=None,
        parameters={},
        topology={"node_count": 1},
        role="entrypoint",
        rank=0,
    )

    assert projection.slug == slug
    assert projection.command == ("serve", "--model", "/models")
    assert projection.architecture == "linux/arm64"
    assert projection.user == "10001:10001"
    assert projection.offline_runtime is True
    assert projection.docker_socket is False
    assert projection.no_new_privileges is True
    assert projection.capabilities == ()
    assert all(mount.read_only for mount in projection.model_mounts)
    assert projection.output_mount.isolated is True
    assert projection.output_mount.read_only is False


def test_registry_fails_closed_for_an_unknown_builtin_slug() -> None:
    with pytest.raises(HarnessCompileError, match="unknown execution harness"):
        HarnessRegistry.with_builtins().compile(
            harness_document("unknown-harness"),
            recipe={"runtime": {"entrypoint": ["serve"]}},
            distribution={"platform": "linux/arm64"},
            patch=None,
            parameters={},
            topology={"node_count": 1},
            role="entrypoint",
            rank=0,
        )


def test_registry_requires_an_exact_schema_v1_harness_document() -> None:
    document = harness_document("vllm")
    document["schema_version"] = True

    with pytest.raises(HarnessCompileError, match="resolved execution harness is invalid"):
        HarnessRegistry.with_builtins().compile(
            document,
            recipe={"runtime": {"entrypoint": ["serve"]}},
            distribution={"platform": "linux/arm64"},
            patch=None,
            parameters={},
            topology={"node_count": 1},
            role="entrypoint",
            rank=0,
        )


def test_custom_adapter_requires_an_exact_signed_source_bundle() -> None:
    registry = HarnessRegistry()
    compiler = SyntheticHarnessCompiler("custom-adapter")
    with pytest.raises(HarnessCompileError, match="signed source bundle"):
        registry.register(compiler)

    registry.register(
        compiler,
        source_bundle={
            "sha256": "a" * 64,
            "expected_bytes": 2048,
            "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
            "signature": "b" * 64,
        },
    )
    document = harness_document("custom-adapter")
    document["source_bundle"]["sha256"] = "c" * 64  # type: ignore[index]
    with pytest.raises(HarnessCompileError, match="exact signed source bundle"):
        registry.compile(
            document,
            recipe={"runtime": {"entrypoint": ["serve"]}},
            distribution={"platform": "linux/arm64"},
            patch=None,
            parameters={},
            topology={"node_count": 1},
            role="entrypoint",
            rank=0,
        )


def test_custom_adapter_cannot_bypass_shell_free_projection_checks() -> None:
    class ShellCompiler(SyntheticHarnessCompiler):
        def compile(self, *args, **kwargs):
            return replace(
                super().compile(*args, **kwargs), command=("sh", "-c", "echo")
            )

    registry = HarnessRegistry()
    registry.register(
        ShellCompiler("custom-shell"),
        source_bundle={
            "sha256": "a" * 64,
            "expected_bytes": 2048,
            "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
            "signature": "b" * 64,
        },
    )
    with pytest.raises(HarnessCompileError, match="unsafe shell syntax"):
        registry.compile(
            harness_document("custom-shell"),
            recipe={"runtime": {"entrypoint": ["serve"]}},
            distribution={"platform": "linux/arm64"},
            patch=None,
            parameters={},
            topology={"node_count": 1},
            role="entrypoint",
            rank=0,
        )

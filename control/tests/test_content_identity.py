"""``content_identity`` decides sameness from content and nothing else."""

from __future__ import annotations

from types import SimpleNamespace

from vonk_control.content_identity import (
    ImageContent,
    differing_image_fields,
    reusable_build,
    same_image,
    same_model_object,
)

DIGEST = "sha256:" + "a" * 64
ARCHIVE = "b" * 64


def _receipt(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "image_digest": DIGEST,
        "oci_archive_sha256": ARCHIVE,
        "image_bytes": 31,
        "architecture": "linux-arm64",
        "runtime_interface": "vonk.runtime.v1",
        "runtime_interface_label": "v1",
        # Provenance: who recorded the image first.
        "build_id": "build-a",
        "distribution_publisher": "publisher-a",
        "distribution_slug": "recipe-a",
        "distribution_content_sha256": "c" * 64,
        "runtime_adapter": "adapter-a",
    }
    return SimpleNamespace(**(values | changes))


def test_the_same_bytes_are_the_same_image_whoever_recorded_them() -> None:
    other = _receipt(
        build_id="build-b",
        distribution_publisher="publisher-b",
        distribution_slug="recipe-b",
        distribution_content_sha256="d" * 64,
        runtime_adapter="adapter-b",
    )
    assert same_image(_receipt(), other)


def test_different_content_is_a_different_image() -> None:
    for field, value in (
        ("image_digest", "sha256:" + "e" * 64),
        ("oci_archive_sha256", "e" * 64),
        ("image_bytes", 32),
        ("architecture", "linux-amd64"),
    ):
        assert not same_image(_receipt(), _receipt(**{field: value})), field
    assert differing_image_fields(_receipt(), _receipt(image_bytes=32)) == (
        "image_bytes",
    )


def test_spellings_and_unstated_fields_are_read_by_content() -> None:
    plan = {"image_digest": DIGEST, "oci_layout_sha256": ARCHIVE, "image_bytes": 31}
    assert same_image(plan, _receipt())
    # A field only one side states does not contradict the other side...
    assert same_image(ImageContent(image_digest=DIGEST), _receipt())
    # ...but records that share no identifying field are not the same image.
    assert not same_image(ImageContent(image_bytes=31), _receipt())
    assert not same_image({}, {})


def test_a_model_object_is_its_digest() -> None:
    receipt = {"sha256": "a" * 64, "bytes": 10, "path": "model-00001.safetensors"}
    renamed = SimpleNamespace(sha256="a" * 64, size_bytes=10, path="other.bin")
    assert same_model_object(receipt, renamed)
    assert not same_model_object(receipt, {"sha256": "b" * 64, "bytes": 10})
    assert not same_model_object(receipt, {"sha256": "a" * 64, "bytes": 11})


def test_a_build_is_reusable_by_executable_inputs_not_by_the_builder_binary() -> None:
    class Resolution:
        source_bundle_sha256 = "s" * 64

        def build_input_for_builder(self, binary_digest: str) -> str:
            return f"input-with-{binary_digest}"

    resolution = Resolution()
    # The build recorded the binary of the builder that produced it; a later
    # agent upgrade does not change the build's executable inputs.
    assert reusable_build(
        resolution,
        build_input_sha256="input-with-old-builder",
        source_bundle_sha256="s" * 64,
        recorded_builder_binary_digest="old-builder",
    )
    assert not reusable_build(
        resolution,
        build_input_sha256="input-with-old-builder",
        source_bundle_sha256="t" * 64,
        recorded_builder_binary_digest="old-builder",
    )
    assert not reusable_build(
        resolution,
        build_input_sha256="input-of-another-executable",
        source_bundle_sha256="s" * 64,
        recorded_builder_binary_digest="old-builder",
    )


def test_damaged_cached_builder_identity_does_not_block_fresh_resolution() -> None:
    """A corrupt cached receipt must be a miss, not a refusal of a fresh build."""
    from vonk_control.recipe_builds import RecipeBuildResolution

    resolution = RecipeBuildResolution(
        recipe_revision_id="recipe",
        recipe_content_sha256="a" * 64,
        source_bundle_sha256="b" * 64,
        input_intent_sha256="c" * 64,
        input_intent={},
    )
    assert not reusable_build(
        resolution,
        build_input_sha256="d" * 64,
        source_bundle_sha256=resolution.source_bundle_sha256,
        recorded_builder_binary_digest="damaged",
    )
    # The same resolution immediately accepts a valid content identity.
    valid = resolution.build_input_for_builder("e" * 64)
    assert valid is not None
    assert reusable_build(
        resolution,
        build_input_sha256=valid,
        source_bundle_sha256=resolution.source_bundle_sha256,
        recorded_builder_binary_digest="e" * 64,
    )

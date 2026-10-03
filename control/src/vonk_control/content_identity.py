"""The one place that decides whether two records are the same image, model or build.

An image is its content: the manifest digest, the OCI archive sha256, the bytes
and the architecture (plus the runtime interface the image declares). A model
object is its digest. A build is reusable by ``build_input_sha256`` computed
from the executable inputs, without the builder binary. Everything else on a
receipt, plan or row says who asked first and is provenance, never identity:
the recipe, slug or revision; ``build_id``; ``distribution_publisher``,
``distribution_slug`` and ``distribution_content_sha256``; the runtime adapter
of the asker. Comparing provenance refuses an identical image the first time a
sibling recipe, an editorial successor or an upgraded builder reaches it.

Every such decision goes through this module, and
``control/tests/content_identity_boundaries.py`` fails the control suite on a
comparison of a provenance field anywhere else unless the site is named, with a
reason, as a real security or ownership edge.

A field that only one side states does not contradict the other side, because
receipts, plans, rows and observations carry different subsets. The digest or
archive must be stated by both sides and equal: two records that share no
identifying field are never the same image.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

_IMAGE_DIGEST_NAMES = (
    "image_digest",
    "oci_image_digest",
    "runtime_image_digest",
    "imported_image_digest",
    "manifest_digest",
)
_ARCHIVE_NAMES = ("oci_archive_sha256", "oci_layout_sha256", "archive_sha256")
_IMAGE_BYTES_NAMES = ("image_bytes", "stored_bytes")
_MODEL_DIGEST_NAMES = ("sha256", "blob_sha256", "digest")
_MODEL_BYTES_NAMES = ("bytes", "size_bytes")


def _read(source: object, names: tuple[str, ...]) -> object | None:
    for name in names:
        value = (
            source.get(name)
            if isinstance(source, Mapping)
            else getattr(source, name, None)
        )
        if value is not None:
            return value
    return None


@dataclass(frozen=True, slots=True)
class ImageContent:
    """What an image is. ``None`` means the record does not state the field."""

    image_digest: str | None = None
    archive_sha256: str | None = None
    image_bytes: int | None = None
    architecture: str | None = None
    runtime_interface: str | None = None
    runtime_interface_label: str | None = None


def image_content(source: object) -> ImageContent:
    """Read the content of an image from a receipt, plan, row or mapping.

    The record may spell the fields differently (``oci_layout_sha256`` in a
    plan, ``oci_archive_sha256`` in a receipt); this is the only place that
    knows every spelling.
    """

    if isinstance(source, ImageContent):
        return source
    image_bytes = _read(source, _IMAGE_BYTES_NAMES)
    return ImageContent(
        image_digest=_text(_read(source, _IMAGE_DIGEST_NAMES)),
        archive_sha256=_text(_read(source, _ARCHIVE_NAMES)),
        image_bytes=image_bytes if type(image_bytes) is int else None,
        architecture=_text(_read(source, ("architecture",))),
        runtime_interface=_text(_read(source, ("runtime_interface",))),
        runtime_interface_label=_text(_read(source, ("runtime_interface_label",))),
    )


def _text(value: object | None) -> str | None:
    return value if isinstance(value, str) else None


_IMAGE_FIELDS = (
    "image_digest",
    "archive_sha256",
    "image_bytes",
    "architecture",
    "runtime_interface",
    "runtime_interface_label",
)


def differing_image_fields(first: object, second: object) -> tuple[str, ...]:
    """Content fields both records state and that differ, for an honest refusal."""

    left = image_content(first)
    right = image_content(second)
    return tuple(
        name
        for name in _IMAGE_FIELDS
        if getattr(left, name) is not None
        and getattr(right, name) is not None
        and getattr(left, name) != getattr(right, name)
    )


def same_image(first: object, second: object) -> bool:
    """Whether two records describe the same image bytes.

    Compares every content field both records state, and requires the image
    digest or the archive digest to be stated by both: records that share no
    identifying field are not the same image.
    """

    left = image_content(first)
    right = image_content(second)
    identified = any(
        getattr(left, name) is not None and getattr(right, name) is not None
        for name in ("image_digest", "archive_sha256")
    )
    return identified and not differing_image_fields(left, right)


def same_model_object(first: object, second: object) -> bool:
    """Whether two records describe the same model file: its digest, and its
    size when both state one.
    """

    a = _read(first, _MODEL_DIGEST_NAMES)
    b = _read(second, _MODEL_DIGEST_NAMES)
    if not isinstance(a, str) or a != b:
        return False
    size_a = _read(first, _MODEL_BYTES_NAMES)
    size_b = _read(second, _MODEL_BYTES_NAMES)
    return size_a is None or size_b is None or size_a == size_b


class BuildResolution(Protocol):
    """The part of a build resolution that decides reuse."""

    @property
    def source_bundle_sha256(self) -> str: ...

    def build_input_for_builder(self, binary_digest: str) -> str: ...


def reusable_build(
    resolution: BuildResolution,
    *,
    build_input_sha256: str,
    source_bundle_sha256: str,
    recorded_builder_binary_digest: str,
) -> bool:
    """Whether a succeeded build's executable inputs equal the resolution's.

    A build's stored input identity includes the binary of the builder that
    produced it. That binary changes whenever the Spark agent is upgraded and is
    not an input of the image, so the resolution's executable intent is bound to
    the build's own recorded builder before the two are compared. Which recipe,
    revision or node asked for the build is never part of the answer.
    """

    return (
        source_bundle_sha256 == resolution.source_bundle_sha256
        and build_input_sha256
        == resolution.build_input_for_builder(recorded_builder_binary_digest)
    )

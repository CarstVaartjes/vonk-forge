"""Platform-owned adaptation of a built recipe image.

A recipe Dockerfile owns the pinned upstream runtime, CUDA/EXL3 compilation,
model patches, model layout and engine arguments.  The platform owns the final
Vonk contract layered onto that built upstream image:

* the ``ai.vonkforge.runtime-interface`` label the agent's OCI policy requires,
* the canonical ``/opt/vonk/bin/<engine>`` launcher, and
* the numeric runtime user plus ownership of the writable output paths.

One reviewed, digest-identified adaptation stage provides that contract instead
of boilerplate every recipe Dockerfile reproduces.  The ``v1`` interface label
only marks compatibility; it does not identify the implementation that was
installed, so the adapter identity is ``adapter_id`` plus ``adapter_sha256``.
The digest covers the canonical adapter definition, and the prepared-image
identity embeds both, so changing the adaptation invalidates a prepared image
by construction.

The adaptation is deliberately not one flattened launcher.  Every engine and
topology resolves through this registry, and an entry either preserves a
reviewed recipe-provided launcher (verified to exist for the runtime user) or
installs the platform launcher.  A behaviour that an upstream revision needs
must therefore be added here as an explicitly reviewed adapter, never as an
arbitrary recipe shell hook.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.build_import import (
    RecipeBuildAdapter,
    RecipeBuildAdapterDefinition,
)

from .harnesses.canonical_metadata import CANONICAL_HARNESS_BY_SLUG
from .runtime_writable_paths import writable_paths

RUNTIME_INTERFACE_LABEL = "ai.vonkforge.runtime-interface"
RUNTIME_INTERFACE_LABEL_VALUE = "v1"
RUNTIME_ADAPTER_LABEL = "ai.vonkforge.runtime-adapter"
RUNTIME_ADAPTER_DIGEST_LABEL = "ai.vonkforge.runtime-adapter-sha256"
ADAPTER_IMAGE_USER = "10001:10001"
ADAPTER_SCHEMA_VERSION = 1

# The adaptation stage builds from the recipe image through an explicit
# argument.  Keeping the image identity in a build argument keeps the adapter
# bytes stable from build to build: the digest identifies the adaptation
# implementation, not one build of it.
_RECIPE_IMAGE_ARGUMENT = "VONK_RECIPE_IMAGE"


class RuntimeAdapterError(ValueError):
    """No reviewed adapter covers the requested engine and topology."""


@dataclass(frozen=True, slots=True)
class _AdapterSpec:
    """One reviewed adaptation implementation, keyed by engine and topology."""

    adapter_id: str
    # ``None`` preserves and verifies a reviewed recipe-provided launcher; a
    # string installs this explicitly reviewed launcher at the canonical path.
    launcher: str | None


def _shared_adapter(engine: str) -> _AdapterSpec:
    # Every current recipe ships its own reviewed launcher (a wrapper that
    # selects a model-specific script or patch), so the shared entry preserves
    # and verifies it.  Installing a launcher here would flatten behaviours
    # that are not equivalent.  Flipping one engine to an installed launcher is
    # intentionally a one-line registry change once its recipes stop shipping
    # their own.
    return _AdapterSpec(f"vonk.runtime-contract.{engine}.v1", None)


# Resolution is by engine *and* topology.  A topology-specific override is a
# reviewed entry here rather than a recipe knob; an unlisted pair fails closed
# below instead of silently inheriting a shared implementation.
_ENGINE_ADAPTERS: dict[str, _AdapterSpec] = {
    engine: _shared_adapter(engine) for engine in CANONICAL_HARNESS_BY_SLUG
}
_TOPOLOGY_ADAPTERS: dict[tuple[str, str], _AdapterSpec] = {}


@dataclass(frozen=True, slots=True)
class RuntimeAdapter:
    """A resolved, immutable adaptation of a built recipe image."""

    adapter_id: str
    engine: str
    image_user: str
    containerfile: str

    @property
    def definition(self) -> RecipeBuildAdapterDefinition:
        return RecipeBuildAdapterDefinition(
            adapter_id=self.adapter_id,
            containerfile=self.containerfile,
            engine=self.engine,
            image_user=self.image_user,
        )

    @property
    def digest(self) -> str:
        """The canonical content digest of this adaptation implementation."""
        return hashlib.sha256(canonical_message(self.definition)).hexdigest()

    def labels(self) -> tuple[tuple[str, str], ...]:
        """The platform labels the final image must carry."""
        return (
            (RUNTIME_INTERFACE_LABEL, RUNTIME_INTERFACE_LABEL_VALUE),
            (RUNTIME_ADAPTER_LABEL, self.adapter_id),
            (RUNTIME_ADAPTER_DIGEST_LABEL, self.digest),
        )

    def document(self) -> dict[str, str]:
        """The executable adapter identity bound into a prepared-image identity."""
        return {"adapter_id": self.adapter_id, "adapter_sha256": self.digest}

    def to_wire(self) -> RecipeBuildAdapter:
        return RecipeBuildAdapter(
            adapter_sha256=self.digest,
            definition=self.definition,
        )


def resolve_runtime_adapter(
    engine: object, topology: object | None = None
) -> RuntimeAdapter:
    """Resolve the one supported adapter for an engine and topology.

    A recipe cannot select an adapter and cannot restate its values: the
    engine slug and topology mode are the only inputs.  An unknown or
    unsupported pair is an explicit failure, never a default.
    """
    if type(engine) is not str or engine not in CANONICAL_HARNESS_BY_SLUG:
        raise RuntimeAdapterError(f"runtime adapter engine is unsupported: {engine}")
    mode: object = None
    if topology is not None:
        mode = _topology_mode(topology)
        if type(mode) is not str or not mode:
            raise RuntimeAdapterError(
                f"runtime adapter topology is invalid: {engine}/{mode}"
            )
    spec = _TOPOLOGY_ADAPTERS.get((engine, mode)) if type(mode) is str else None
    if spec is None:
        spec = _ENGINE_ADAPTERS[engine]
    return RuntimeAdapter(
        adapter_id=spec.adapter_id,
        engine=engine,
        image_user=ADAPTER_IMAGE_USER,
        containerfile=render_adaptation_stage(engine, launcher=spec.launcher),
    )


def render_adaptation_stage(engine: str, *, launcher: str | None) -> str:
    """Render the ordered adaptation stage for one engine.

    The stage is deliberately data-free: it derives the writable directories
    from the central platform contract, so a recipe cannot move a cache root
    and the adapter cannot drift from the runtime environment contract.
    """
    harness = CANONICAL_HARNESS_BY_SLUG.get(engine)
    if harness is None:
        raise RuntimeAdapterError(f"runtime adapter engine is unsupported: {engine}")
    wrapper = harness.wrapper
    uid, _, gid = ADAPTER_IMAGE_USER.partition(":")
    directories = _adapter_directories(engine)
    steps = [
        f"ARG {_RECIPE_IMAGE_ARGUMENT}",
        f"FROM ${{{_RECIPE_IMAGE_ARGUMENT}}}",
        "USER 0",
        "RUN set -eu \\",
        f" && install --directory --mode=0755 {PurePosixPath(wrapper).parent} \\",
        " && install --directory "
        f"--owner={uid} --group={gid or uid} " + " ".join(directories) + " \\",
    ]
    if launcher is None:
        steps.append(f" && test -x {wrapper}")
    else:
        encoded = base64.b64encode(launcher.encode("utf-8")).decode("ascii")
        steps.extend(
            (
                f" && printf '%s' '{encoded}' | base64 -d > {wrapper} \\",
                f" && chmod 0755 {wrapper}",
            )
        )
    steps.extend((f"USER {ADAPTER_IMAGE_USER}", "WORKDIR /tmp"))
    return "\n".join(steps) + "\n"


def _adapter_directories(engine: str) -> tuple[str, ...]:
    directories = ["/outputs", *(item.path for item in writable_paths(engine))]
    return tuple(dict.fromkeys(directories))


def _topology_mode(topology: object) -> object:
    if isinstance(topology, Mapping):
        return topology.get("mode")
    return getattr(topology, "mode", None)


__all__ = [
    "ADAPTER_IMAGE_USER",
    "ADAPTER_SCHEMA_VERSION",
    "RUNTIME_ADAPTER_DIGEST_LABEL",
    "RUNTIME_ADAPTER_LABEL",
    "RUNTIME_INTERFACE_LABEL",
    "RUNTIME_INTERFACE_LABEL_VALUE",
    "RuntimeAdapter",
    "RuntimeAdapterError",
    "render_adaptation_stage",
    "resolve_runtime_adapter",
]

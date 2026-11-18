"""The closed state words of Controller-only stored records.

These records are Controller-only, so their enums live here and not in
``vonk_agent_protocol.state_machines`` (whose machines are published through
``wire.json`` into Rust, OpenAPI and TypeScript).  Each is a ``WireEnum`` so its
CHECK is generated with ``machine_check``.  Recipe build words are the exception:
they are also published through the run-switch contract
(``RunSwitchContainerBuildState``, ``run_switch_contract/vocabulary.py``), a second
spelling of ``RecipeBuildState`` to be built from the enum later.
"""

from __future__ import annotations

from vonk_agent_protocol.wire_model import WireEnum


class CatalogRevisionState(WireEnum):
    """The standing of one catalog document revision."""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    FAILED = "failed"


class RecipeBuildState(WireEnum):
    """The standing of one recipe container build."""

    PLANNED = "planned"
    BUILDING = "building"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class LibrarySyncRunState(WireEnum):
    """The standing of one recipe library synchronization run."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ClusterMappingState(WireEnum):
    """The standing of one cluster mapping of a recipe revision onto Sparks."""

    PLANNED = "planned"
    READY = "ready"
    STALE = "stale"

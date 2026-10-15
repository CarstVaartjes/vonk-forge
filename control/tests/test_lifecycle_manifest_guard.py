"""Every lifecycle parent whose children need a host-runtime grant records them.

The host-runtime authority grants a container Start or Stop only to a child its
parent's manifest (``phases``) names exactly. A producer that omits the
manifest queues children that can never be granted, so the queue refuses it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from vonk_agent_protocol import ContainerRuntimeAction
from vonk_control.host_helper_authority import HostRuntimeAuthorityService
from vonk_control.recipe_operations.constants import _MANIFEST_BOUND_KINDS
from vonk_control.recipe_operations.errors import RecipeRequestInvalid
from vonk_control.recipe_operations.persistence import PersistenceMixin


def test_manifest_bound_kinds_are_the_granted_start_and_stop_kinds() -> None:
    kinds = HostRuntimeAuthorityService._ACTION_KINDS
    assert _MANIFEST_BOUND_KINDS == (
        kinds[ContainerRuntimeAction.START] | kinds[ContainerRuntimeAction.STOP]
    )


@pytest.mark.parametrize("kind", sorted(_MANIFEST_BOUND_KINDS))
def test_a_granted_kind_without_its_manifest_is_refused_before_any_write(
    kind: str,
) -> None:
    with pytest.raises(RecipeRequestInvalid):
        PersistenceMixin()._queue_in_session(
            None,  # type: ignore[arg-type]  # refused before the session is used
            kind=kind,
            owner_kind="run",
            owner_id="10000000-0000-4000-8000-000000000001",
            plan_digest="sha256:" + "a" * 64,
            actor="test",
            request_id="request-1",
            node_payloads=(("spark-1", {}),),
            authority_digest="sha256:" + "b" * 64,
            now=datetime(2026, 10, 15, tzinfo=UTC),
        )

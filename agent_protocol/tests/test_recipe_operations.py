from __future__ import annotations

import pytest
from vonk_agent_protocol import (
    AgentOperation,
    AgentProtocolError,
    RecipeOperationRequest,
)

INSTALL = {
    "schema_version": 1,
    "installation_id": "00000000-0000-4000-8000-000000000001",
    "recipe_revision_id": "00000000-0000-4000-8000-000000000002",
    "recipe_content_sha256": "a" * 64,
    "mapping_id": "00000000-0000-4000-8000-000000000007",
    "mapping_generation": 1,
    "recipe_build_id": "00000000-0000-4000-8000-000000000008",
    "image_digest": "sha256:" + "d" * 64,
    "rank": 0,
    "role": "entrypoint",
    "plan_digest": "b" * 64,
    "expected_bytes": 100,
}
START = {
    "schema_version": 1,
    "run_id": "00000000-0000-4000-8000-000000000003",
    "installation_id": INSTALL["installation_id"],
    "recipe_revision_id": INSTALL["recipe_revision_id"],
    "recipe_content_sha256": "a" * 64,
    "mapping_id": INSTALL["mapping_id"],
    "mapping_generation": 1,
    "image_digest": INSTALL["image_digest"],
    "plan_digest": "c" * 64,
    "alias": "qwen3",
    "rank": 0,
    "role": "entrypoint",
    "port": 8000,
    "reserved_memory_bytes": 200,
    "endpoint_address": "192.168.1.211",
    "world_size": 1,
    "local_address": None,
    "master_address": None,
    "master_port": None,
}
STOP = {
    "schema_version": 1,
    "run_id": START["run_id"],
    "plan_digest": START["plan_digest"],
}
UNINSTALL = {
    "schema_version": 1,
    "installation_id": INSTALL["installation_id"],
    "recipe_content_sha256": INSTALL["recipe_content_sha256"],
    "plan_digest": INSTALL["plan_digest"],
}


def test_recipe_operation_vocabulary_is_closed() -> None:
    assert {
        operation.value
        for operation in AgentOperation
        if operation.value.startswith("recipe.")
    } == {
        "recipe.build.v1",
        "recipe.image.import.v1",
        "recipe.install",
        "recipe.start",
        "recipe.job.run.v1",
        "recipe.stop",
        "recipe.uninstall",
    }


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        (AgentOperation.RECIPE_INSTALL, INSTALL),
        (AgentOperation.RECIPE_START, START),
        (AgentOperation.RECIPE_STOP, STOP),
        (AgentOperation.RECIPE_UNINSTALL, UNINSTALL),
    ],
)
def test_recipe_operation_payloads_are_typed_and_digest_bound(
    operation: AgentOperation, payload: dict[str, object]
) -> None:
    request = RecipeOperationRequest.parse(operation, payload)

    assert request.operation is operation
    assert request.plan_digest == payload["plan_digest"]


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        (AgentOperation.RECIPE_INSTALL, INSTALL | {"shell": "curl evil"}),
        (AgentOperation.RECIPE_START, START | {"environment": {"TOKEN": "x"}}),
        (AgentOperation.RECIPE_STOP, STOP | {"plan_digest": "not-a-digest"}),
        (AgentOperation.RECIPE_UNINSTALL, UNINSTALL | {"host_path": "/tmp"}),
    ],
)
def test_recipe_operations_reject_hacks_unknown_fields_and_weak_identity(
    operation: AgentOperation, payload: dict[str, object]
) -> None:
    with pytest.raises(AgentProtocolError):
        RecipeOperationRequest.parse(operation, payload)


def test_recipe_start_accepts_tailnet_address_but_rejects_localhost() -> None:
    request = RecipeOperationRequest.parse(
        AgentOperation.RECIPE_START,
        START | {"endpoint_address": "100.100.20.30"},
    )
    assert request.endpoint_address == "100.100.20.30"

    with pytest.raises(AgentProtocolError, match="endpoint address"):
        RecipeOperationRequest.parse(
            AgentOperation.RECIPE_START,
            START | {"endpoint_address": "127.0.0.1"},
        )


def test_multinode_start_requires_explicit_direct_fabric_rendezvous() -> None:
    request = RecipeOperationRequest.parse(
        AgentOperation.RECIPE_START,
        START
        | {
            "world_size": 2,
            "local_address": "192.168.100.3",
            "master_address": "192.168.100.2",
            "master_port": 29500,
            "rank": 1,
            "role": "worker",
        },
    )
    assert request.master_address == "192.168.100.2"

    large = RecipeOperationRequest.parse(
        AgentOperation.RECIPE_START,
        START
        | {
            "world_size": 17,
            "local_address": "192.168.100.3",
            "master_address": "192.168.100.2",
            "master_port": 29500,
        },
    )
    assert large.world_size == 17

    with pytest.raises(AgentProtocolError, match="fabric"):
        RecipeOperationRequest.parse(
            AgentOperation.RECIPE_START,
            START
            | {
                "world_size": 2,
                "local_address": None,
                "master_address": "192.168.100.2",
                "master_port": 29500,
            },
        )

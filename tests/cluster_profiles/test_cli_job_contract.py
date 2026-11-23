"""``vonkctl job contract`` reads a recipe's job contract before a job."""

from __future__ import annotations

import json

import pytest

from cluster_profiles import cli, cli_artifact_jobs
from cluster_profiles.cli_render.artifact_jobs import contract_knob_fields
from cluster_profiles.cli_states_generated import JobContractState
from cluster_profiles.control_client.errors import ControlNotFound
from cluster_profiles.generated_control.models.artifact_job_contract_response import (
    ArtifactJobContractResponse,
)
from cluster_profiles.generated_control.types import Unset

RUN_ID = "00000000-0000-4000-8000-000000000002"
REVISION_ID = "00000000-0000-4000-8000-000000000007"
CONTRACT_PATH = "/api/artifact-jobs/contract"


def _slot(slot_id: str, media_type: str, extension: str, limit: int) -> dict:
    return {
        "id": slot_id,
        "label": slot_id.title(),
        "description": f"The {slot_id}.",
        "media_types": [media_type],
        "extensions": [extension],
        "min_files": 1,
        "max_files": 1,
        "max_file_bytes": limit,
        "max_total_bytes": limit,
    }


def _contract_document(*, with_run: bool = True) -> dict[str, object]:
    document: dict[str, object] = {
        "state": "available",
        "recipe_revision_id": REVISION_ID,
        "recipe": "vonk-forge/flux-klein",
        "contract_sha256": "a" * 64,
        "contract": {
            "schema_version": 1,
            "interface": "image-job",
            "input": {
                "required": True,
                "media_types": ["text/plain"],
                "max_bytes": 16384,
                "slots": [_slot("prompt", "text/plain", ".txt", 16384)],
            },
            "parameters": [
                {
                    "name": "steps",
                    "type": "integer",
                    "default": 4,
                    "minimum": None,
                    "maximum": None,
                    "allowed_values": [],
                    "pattern": None,
                }
            ],
            "output": {
                "path": "/outputs",
                "max_total_bytes": 16777216,
                "slots": [_slot("image", "image/png", ".png", 16777216)],
            },
            "output_limits": {
                "max_files": 1,
                "max_file_bytes": 16777216,
                "max_total_bytes": 16777216,
                "allowed_media_types": ["image/png"],
            },
            "max_timeout_seconds": 3600,
        },
    }
    if with_run:
        document |= {"run_id": RUN_ID, "alias": "image", "run_state": "running"}
    return document


class _Controller:
    """Answers the contract read like the Controller: a document or a typed refusal."""

    request_timeout_seconds = 15.0

    def __init__(self, outcome: dict[str, object] | Exception) -> None:
        self.outcome = outcome
        self.paths: list[str] = []

    def request(self, method, path, payload=None, **_ignored):
        assert method == "GET"
        assert path.startswith(CONTRACT_PATH + "/")
        self.paths.append(path)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def upload_file(self, *_args, **_kwargs):
        raise AssertionError("a contract read transfers no bytes")

    download_file = upload_file


@pytest.mark.parametrize(
    ("flags", "path"),
    [
        (("--run", RUN_ID), f"{CONTRACT_PATH}/run/{RUN_ID}"),
        (("--revision", REVISION_ID), f"{CONTRACT_PATH}/revision/{REVISION_ID}"),
    ],
)
def test_contract_json_is_the_controllers_typed_document(capsys, flags, path) -> None:
    controller = _Controller(_contract_document())

    status = cli.main(("--json", "job", "contract", *flags), control_client=controller)

    assert status == 0
    assert controller.paths == [path]
    assert json.loads(capsys.readouterr().out) == _contract_document()


def _unavailable(state: JobContractState) -> dict[str, object]:
    """The Controller's typed answer when there is no contract to give."""
    return {
        "state": state.value,
        "detail": f"no contract: {state.value}",
        "recipe_revision_id": REVISION_ID,
        "recipe": "vonk-forge/flux-klein",
    }


UNAVAILABLE = [
    state for state in JobContractState if state is not JobContractState.AVAILABLE
]


def test_every_state_without_a_contract_has_a_next_step() -> None:
    assert set(UNAVAILABLE) == set(cli_artifact_jobs._CONTRACT_NEXT_STEPS)
    assert all(cli_artifact_jobs._CONTRACT_NEXT_STEPS.values())


@pytest.mark.parametrize("state", UNAVAILABLE)
def test_contract_that_cannot_be_given_is_typed_with_a_next_step_after_one_read(
    capsys, state
) -> None:
    controller = _Controller(_unavailable(state))

    status = cli.main(
        ("--json", "job", "contract", "--run", RUN_ID),
        control_client=controller,
    )

    error = json.loads(capsys.readouterr().out)
    assert status != 0
    assert controller.paths == [f"{CONTRACT_PATH}/run/{RUN_ID}"]
    assert error["code"] == "job_contract." + state.value
    assert error["recovery_actions"] == list(
        cli_artifact_jobs._CONTRACT_NEXT_STEPS[state]
    )
    assert error["detail"]


def test_contract_of_an_unknown_run_is_not_found_after_one_read(capsys) -> None:
    controller = _Controller(ControlNotFound(404, "run does not exist"))

    status = cli.main(
        ("--json", "job", "contract", "--run", RUN_ID),
        control_client=controller,
    )

    error = json.loads(capsys.readouterr().out)
    assert status != 0
    assert len(controller.paths) == 1
    assert "vonkctl fleet" in error["recovery_actions"]


@pytest.mark.parametrize("flags", [(), ("--run", RUN_ID, "--revision", REVISION_ID)])
def test_contract_needs_exactly_one_key(vonkctl_json, flags) -> None:
    controller = _Controller(_contract_document())

    status, error = vonkctl_json("job", "contract", *flags, control_client=controller)

    assert status == 2
    assert controller.paths == []
    assert error["recovery_actions"] == ["vonkctl job contract --help"]


def test_contract_knob_fields_are_only_what_the_knob_sets() -> None:
    document = _contract_document()
    contract = document["contract"]
    assert isinstance(contract, dict)
    knobs = [
        {
            "name": "steps",
            "type": "integer",
            "default": 31,
            "minimum": 2,
            "maximum": 51,
            "step": 1,
            "label": "Inference steps",
            "unit": "steps",
            "description": "More steps, finer detail.",
            "allowed_values": [],
        },
        {"name": "frames", "type": "enum", "default": 49, "allowed_values": [49, 97]},
    ]
    contract["parameters"] = knobs
    parsed = ArtifactJobContractResponse.from_dict(document)
    assert not isinstance(parsed.contract, Unset | None)
    ranged, enum = (contract_knob_fields(k) for k in parsed.contract.parameters)

    assert dict(ranged) == {
        "Knob": "steps",
        "Knob label": "Inference steps",
        "Knob type": "integer",
        "Knob default": 31,
        "Knob range": "2 to 51 in steps of 1",
        "Knob unit": "steps",
        "Knob description": "More steps, finer detail.",
    }
    assert dict(enum) == {
        "Knob": "frames",
        "Knob type": "enum",
        "Knob default": 49,
        "Knob allowed values": "49, 97",
    }

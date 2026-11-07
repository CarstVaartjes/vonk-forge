"""``vonkctl recipe job contract`` reads a recipe's job contract before a job."""

from __future__ import annotations

import json

import pytest

from cluster_profiles import cli, cli_artifact_jobs
from cluster_profiles.cli_states_generated import JobContractState
from cluster_profiles.control_client import ControlNotFound

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

    status = cli.main(
        ("--json", "recipe", "job", "contract", *flags), control_client=controller
    )

    assert status == 0
    assert controller.paths == [path]
    assert json.loads(capsys.readouterr().out) == _contract_document()


def test_contract_text_names_slots_limits_and_knobs(capsys) -> None:
    status = cli.main(
        ("recipe", "job", "contract", "--run", RUN_ID),
        control_client=_Controller(_contract_document()),
    )

    output = capsys.readouterr().out
    assert status == 0
    assert "Interface: image-job" in output
    assert "Input slot: prompt" in output
    assert "Input slot media types: text/plain" in output
    assert "Input slot max file size: 16.0 KiB (16384 bytes)" in output
    assert "Output slot: image" in output
    assert "Output slot extensions: .png" in output
    assert "Knob: steps" in output
    assert f"vonkctl recipe job create --run {RUN_ID}" in output


def test_contract_by_revision_reads_without_a_run(capsys) -> None:
    status = cli.main(
        ("recipe", "job", "contract", "--revision", REVISION_ID),
        control_client=_Controller(_contract_document(with_run=False)),
    )

    output = capsys.readouterr().out
    assert status == 0
    assert "Run:" not in output
    assert "Recipe revision: " + REVISION_ID in output


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
        ("--json", "recipe", "job", "contract", "--run", RUN_ID),
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
        ("--json", "recipe", "job", "contract", "--run", RUN_ID),
        control_client=controller,
    )

    error = json.loads(capsys.readouterr().out)
    assert status != 0
    assert len(controller.paths) == 1
    assert "vonkctl fleet" in error["recovery_actions"]


@pytest.mark.parametrize("flags", [(), ("--run", RUN_ID, "--revision", REVISION_ID)])
def test_contract_needs_exactly_one_key(capsys, flags) -> None:
    controller = _Controller(_contract_document())

    status = cli.main(("recipe", "job", "contract", *flags), control_client=controller)

    assert status == 2
    assert controller.paths == []
    assert "Next: vonkctl recipe job contract --help" in capsys.readouterr().err

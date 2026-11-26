"""The job contract of a run or revision, and the --input/--option binding built from it."""

from __future__ import annotations

import argparse
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cluster_profiles.cli_artifact_jobs.client import (
    ArtifactJobClient,
    _recover,
    _transient_failure,
)
from cluster_profiles.cli_artifact_jobs.values import _integer, _mapping
from cluster_profiles.cli_render.artifact_jobs import Knob
from cluster_profiles.cli_states_generated import JobContractState
from cluster_profiles.control_client.errors import (
    ControlHTTPError,
    ControlMalformedResponse,
    ControlNotFound,
)
from cluster_profiles.control_client.schema import validate_control_document
from cluster_profiles.generated_control.models.artifact_job_contract_response import (
    ArtifactJobContractResponse,
)
from cluster_profiles.generated_control.models.boolean_parameter import BooleanParameter
from cluster_profiles.generated_control.models.enum_parameter import EnumParameter
from cluster_profiles.generated_control.models.float_parameter import FloatParameter
from cluster_profiles.generated_control.models.integer_parameter import IntegerParameter
from cluster_profiles.generated_control.types import Unset

#: Where a contract that cannot be given points next, per state the Controller
#: answers: `profile endpoint` shows whether a load is still in flight, and a
#: revision's contract needs no run.
_CONTRACT_NEXT_STEPS = {
    JobContractState.NOT_RUNNING: (
        "vonkctl profile endpoint",
        "vonkctl job contract --revision <recipe-revision-id>",
    ),
    JobContractState.NOT_A_JOB_RECIPE: ("vonkctl recipe detail <recipe-revision-id>",),
    JobContractState.CONTRACT_INVALID: ("vonkctl recipe library",),
}


_CONTRACT_NOT_FOUND_STEPS = ("vonkctl fleet", "vonkctl profile list")


def _contract(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    quote: Callable[[str], str],
):
    """One bounded read; the Controller's typed answer is final."""
    by, key = ("run", args.run) if args.run else ("revision", args.revision)
    try:
        value = client.request("GET", f"/api/artifact-jobs/contract/{by}/{quote(key)}")
    except ControlNotFound as error:
        error.recovery = _CONTRACT_NOT_FOUND_STEPS
        raise
    answer = validate_control_document("ArtifactJobContractResponse", value)
    if answer.get("contract") is None:
        # The Controller answered that there is no contract to give; the usual
        # error document carries the state as its code, the cause and next steps.
        state = JobContractState(answer["state"])
        raise ControlHTTPError(
            409,
            str(answer.get("detail")),
            code="job_contract." + state.value,
            recovery=_CONTRACT_NEXT_STEPS[state],
        )
    return answer


def _capabilities(client: ArtifactJobClient) -> dict[str, object]:
    def read(timeout: float):
        value = client.request(
            "GET", "/api/artifact-jobs/capabilities", timeout_seconds=timeout
        )
        return validate_control_document("ArtifactJobCapabilitiesResponse", value)

    return _recover(client, read, lambda _timeout: None, retry_if=_transient_failure)


def _knob_value(knob: Knob, raw: str) -> object:
    """The typed value of ``--option``. An enum names its allowed values when the
    text matches none; range and step stay with the Controller."""
    if isinstance(knob, EnumParameter):
        for member in knob.allowed_values:
            if str(member) == raw:
                return member
        listed = ", ".join(str(member) for member in knob.allowed_values)
        raise ValueError(f"option '{knob.name}' takes one of: {listed}; got '{raw}'")
    try:
        if isinstance(knob, IntegerParameter):
            return int(raw)
        if isinstance(knob, FloatParameter):
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError(raw)
            return value
        if isinstance(knob, BooleanParameter):
            if raw.lower() not in {"true", "false"}:
                raise ValueError(raw)
            return raw.lower() == "true"
    except ValueError:
        raise ValueError(
            f"option '{knob.name}' takes a {knob.type_} value, got '{raw}'"
        ) from None
    return raw


def _binding_from_flags(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    quote: Callable[[str], str],
    capabilities: Any,
):
    """The create object and input paths for ``--input``/``--option``, checked
    against the run's job contract (slot names and counts, knob names and types)."""
    args.revision = None
    answer = ArtifactJobContractResponse.from_dict(_contract(args, client, quote))
    contract = answer.contract
    if contract is None or isinstance(contract, Unset):
        raise ControlMalformedResponse("job contract observation is unavailable")
    slots = {slot.id: slot for slot in contract.input_.slots}
    chosen: dict[str, list[Path]] = {slot_id: [] for slot_id in slots}
    for item in args.input:
        slot_id, separator, raw_path = item.partition("=")
        if not separator or not raw_path:
            raise ValueError(f"--input takes SLOT=PATH, got '{item}'")
        if slot_id not in slots:
            raise ValueError(
                f"unknown input slot '{slot_id}'; this recipe takes: "
                + ", ".join(slots)
            )
        chosen[slot_id].append(Path(raw_path).absolute())
    inputs = []
    paths: dict[str, Path] = {}
    for slot_id in slots:
        for path in chosen[slot_id]:
            if path.name in paths:
                raise ValueError(f"two inputs are named {path.name}")
            paths[path.name] = path
            inputs.append({"slot": slot_id, "name": path.name})
    knobs = {knob.name: knob for knob in contract.parameters}
    parameters = {}
    for item in args.option:
        key, separator, raw_value = item.partition("=")
        if not separator:
            raise ValueError(f"--option takes KEY=VALUE, got '{item}'")
        if key not in knobs:
            raise ValueError(
                f"unknown option '{key}'; this recipe takes: " + ", ".join(knobs)
            )
        parameters[key] = _knob_value(knobs[key], raw_value)
    transport = _mapping(capabilities.get("transport"), "artifact job capabilities")
    limits = contract.output_limits
    output_limits = {
        "max_files": min(
            limits.max_files, _integer(transport.get("max_output_files"), "limit")
        ),
        "max_file_bytes": min(
            limits.max_file_bytes,
            _integer(transport.get("max_output_file_bytes"), "limit"),
        ),
        "max_total_bytes": min(
            limits.max_total_bytes,
            _integer(transport.get("max_output_total_bytes"), "limit"),
        ),
        "allowed_media_types": limits.allowed_media_types,
    }
    create = {
        "interface": str(contract.interface),
        "parameters": parameters,
        "inputs": inputs,
        "output_limits": output_limits,
        "timeout_seconds": min(
            contract.max_timeout_seconds,
            _integer(transport.get("max_timeout_seconds"), "job timeout limit"),
        ),
    }
    return create, paths

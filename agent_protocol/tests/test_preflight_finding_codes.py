"""A runtime preflight finding code is a closed contract word; old free text is adopted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    RETIRED_FINDING_CODE_SPELLINGS,
    HelperErrorCode,
    RuntimePreflightFindingCode,
    adopt_preflight_finding_code,
)
from vonk_agent_protocol.runtime_preflight import RuntimePreflightFinding

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "preflight_finding."

#: The words agents wrote before the enum existed: the bare spelling, the
#: kebab-case spelling of a podman or probe diagnostic, and the `helper_<code>`
#: family of a privileged-helper failure.
OLD_AGENT_WORDS = {
    "available": RuntimePreflightFindingCode.AVAILABLE,
    "architecture_mismatch": RuntimePreflightFindingCode.ARCHITECTURE_MISMATCH,
    "disk_reserve_insufficient": RuntimePreflightFindingCode.DISK_RESERVE_INSUFFICIENT,
    "runroot_exceeds_50_bytes": RuntimePreflightFindingCode.RUNROOT_EXCEEDS_50_BYTES,
    "signed_helper_probe_required": (
        RuntimePreflightFindingCode.SIGNED_HELPER_PROBE_REQUIRED
    ),
    "proc-mount-denied": RuntimePreflightFindingCode.PROC_MOUNT_DENIED,
    "user-service-manager-unavailable": (
        RuntimePreflightFindingCode.USER_SERVICE_MANAGER_UNAVAILABLE
    ),
    "unclassified-podman-build-failure": (
        RuntimePreflightFindingCode.UNCLASSIFIED_PODMAN_BUILD_FAILURE
    ),
    "helper_io_failed": RuntimePreflightFindingCode.HELPER_IO_FAILED,
    "helper_grant_invalid": RuntimePreflightFindingCode.HELPER_GRANT_INVALID,
    "helper_operation_io": RuntimePreflightFindingCode.HELPER_OPERATION_IO,
    "helper_probe_invalid_result": (
        RuntimePreflightFindingCode.HELPER_PROBE_INVALID_RESULT
    ),
}


@pytest.mark.parametrize(("old", "member"), OLD_AGENT_WORDS.items())
def test_a_free_text_code_from_an_older_agent_reads_as_its_member(
    old: str, member: RuntimePreflightFindingCode
) -> None:
    assert adopt_preflight_finding_code(old) is member
    finding = RuntimePreflightFinding(
        capability="podman_build", status="failed", code=old
    )
    assert finding.finding_code is member


def test_every_member_reads_as_itself_and_none_is_a_bare_word() -> None:
    for member in RuntimePreflightFindingCode:
        assert member.value.startswith(PREFIX)
        assert adopt_preflight_finding_code(member.value) is member


def test_every_retired_spelling_adopts_to_its_member() -> None:
    for old, member in RETIRED_FINDING_CODE_SPELLINGS.items():
        assert adopt_preflight_finding_code(old) is member
    # Every member has a retired (bare) spelling an older agent could have sent.
    assert set(RETIRED_FINDING_CODE_SPELLINGS.values()) == set(
        RuntimePreflightFindingCode
    )


def test_a_word_no_member_spells_is_kept_as_unclassified_not_refused() -> None:
    assert (
        adopt_preflight_finding_code("a_newer_agents_code")
        is RuntimePreflightFindingCode.UNCLASSIFIED
    )
    # The result still validates: the wire keeps the finding rather than dropping it.
    finding = RuntimePreflightFinding(
        capability="podman_build", status="failed", code="a_newer_agents_code"
    )
    assert finding.code == "a_newer_agents_code"


def test_a_helper_finding_exists_for_every_helper_word_the_runtime_boundary_names() -> (
    None
):
    # `helper_<code>` is how a failed privileged call is reported; the member
    # must exist for each helper code that call can name.
    runtime_words = {
        HelperErrorCode.OPERATION_IO,
        HelperErrorCode.RUNTIME_PROCESS_EXITED,
        HelperErrorCode.REQUEST_INVALID,
        HelperErrorCode.MESSAGE_FRAMING_INVALID,
    }
    for word in runtime_words:
        assert RuntimePreflightFindingCode(f"{PREFIX}helper_{word.value}")


def test_the_wire_schema_publishes_the_finding_and_helper_codes() -> None:
    schema = json.loads(
        (ROOT / "rust/crates/vonk-agent-protocol/schema/wire.json").read_text()
    )
    assert schema["$defs"]["RuntimePreflightFindingCode"]["enum"] == [
        member.value for member in RuntimePreflightFindingCode
    ]
    assert schema["$defs"]["HelperErrorCode"]["enum"] == [
        member.value for member in HelperErrorCode
    ]

from cluster_profiles.availability_presentation import (
    parse_availability_failure,
    parse_availability_operation,
    select_availability_operation,
)


def test_parses_aggregate_children_and_redacts_logs() -> None:
    operation = parse_availability_operation({
        "id": "operation-1",
        "request_id": "request-1",
        "recipe_revision_id": "recipe-revision-1",
        "state": "running",
        "attempt": 2,
        "progress": {"phase": "prepare", "completed_bytes": 120, "total_bytes_known": False},
        "children": [
            {"kind": "model-cache", "state": "running", "progress": {"phase": "download", "completed_bytes": 80}},
            {"kind": "runtime-image", "state": "failed", "progress": {"phase": "build", "completed_bytes": 0}, "failure": {"code": "build_failed", "detail": "build step failed", "recovery_actions": ["retry"], "retryable": True, "log_excerpt": "Authorization: Bearer secret"}},
        ],
    })

    assert operation is not None
    assert operation.recipe_revision_id == "recipe-revision-1"
    assert [member.key for member in operation.members] == ["model-cache", "runtime-image"]
    assert operation.members[1].failure is not None
    assert operation.members[1].failure.log_excerpt == "Authorization: Bearer <redacted>"


def test_exact_failure_fields_include_capacity_and_access_action() -> None:
    failure = parse_availability_failure({
        "code": "access_required",
        "detail": "Hugging Face access is required",
        "recovery_actions": ["open_model_access", "configure_hf_token", "check_access_and_resume"],
        "retryable": False,
        "required_bytes": 200,
        "free_bytes": 100,
        "shortfall_bytes": 100,
    })
    assert failure is not None
    assert failure.recovery_actions[-1] == "check_access_and_resume"
    assert (failure.required_bytes, failure.free_bytes, failure.shortfall_bytes) == (200, 100, 100)
    assert failure.retryable is False


def test_resume_selection_is_exact_revision_scoped_and_prefers_active_attempt() -> None:
    values = [
        parse_availability_operation({"id": "old", "request_id": "old", "recipe_revision_id": "revision-a", "state": "succeeded", "attempt": 3, "progress": {}}),
        parse_availability_operation({"id": "active", "request_id": "active", "recipe_revision_id": "revision-a", "state": "running", "attempt": 1, "progress": {}}),
        parse_availability_operation({"id": "wrong", "request_id": "wrong", "recipe_revision_id": "revision-b", "state": "running", "attempt": 9, "progress": {}}),
    ]
    selected = select_availability_operation([value for value in values if value is not None], "revision-a")
    assert selected is not None and selected.id == "active"
    assert select_availability_operation([value for value in values if value is not None], "revision-c") is None

"""Exercise the Controller producer through its generated client parser."""

def test_generated_python_list_jobs_preserves_cursor_and_typed_rejection() -> None:
    import httpx
    from vonk_control.operation_api import RequestValidationProblem as ProblemProducer

    from cluster_profiles.generated_control.api.default import list_jobs
    from cluster_profiles.generated_control.client import Client
    from cluster_profiles.generated_control.models.request_validation_problem import (
        RequestValidationProblem,
    )

    cursor = "v1.authenticated.boundary"
    kwargs = list_jobs._get_kwargs(
        cursor=cursor,
        limit=20,
        status="queued",
        target="spk_" + "1" * 32,
    )
    assert kwargs["params"] == {
        "cursor": cursor,
        "limit": 20,
        "status": "queued",
        "target": "spk_" + "1" * 32,
    }

    parsed = list_jobs._parse_response(
        client=Client(base_url="https://control.invalid"),
        response=httpx.Response(
            422,
            json=ProblemProducer(detail="job cursor is invalid", issues=[]).model_dump(mode="json"),
        ),
    )
    assert isinstance(parsed, RequestValidationProblem)
    assert parsed.detail == "job cursor is invalid"

"""Exercise the Controller producer through its generated client parser."""


def test_generated_python_list_operations_keeps_cursor_and_typed_rejection() -> None:
    import httpx2
    from vonk_control.operation_api import RequestValidationProblem as ProblemProducer

    from cluster_profiles.generated_control.api.default import list_operations
    from cluster_profiles.generated_control.client import Client
    from cluster_profiles.generated_control.models.request_validation_problem import (
        RequestValidationProblem,
    )

    cursor = "v1.authenticated.boundary"
    kwargs = list_operations._get_kwargs(
        cursor=cursor,
        limit=20,
        state="queued",
        node_id="spk_" + "1" * 32,
    )
    assert kwargs["params"] == {
        "cursor": cursor,
        "limit": 20,
        "state": "queued",
        "node_id": "spk_" + "1" * 32,
    }

    parsed = list_operations._parse_response(
        client=Client(base_url="https://control.invalid"),
        response=httpx2.Response(
            422,
            json=ProblemProducer(
                detail="operation cursor is invalid", issues=[]
            ).model_dump(mode="json"),
        ),
    )
    assert isinstance(parsed, RequestValidationProblem)
    assert parsed.detail == "operation cursor is invalid"

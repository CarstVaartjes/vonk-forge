"""Exercise the Controller producer through its generated HTTP client."""


def test_generated_python_list_operations_keeps_cursor_and_recovers_after_rejection() -> (
    None
):
    import httpx2
    from vonk_control.operation_api import OperationsResponse
    from vonk_control.operation_api import RequestValidationProblem as ProblemProducer

    from cluster_profiles.generated_control.api.default import list_operations
    from cluster_profiles.generated_control.client import AuthenticatedClient

    cursor = "v1.authenticated.boundary"
    observed = []
    problem = ProblemProducer(detail="operation cursor is invalid", issues=[])

    def peer(request):
        observed.append(request)
        if request.url.params.get("cursor") == cursor:
            return httpx2.Response(422, content=problem.model_dump_json().encode())
        return httpx2.Response(
            200,
            content=OperationsResponse(operations=[], total=0)
            .model_dump_json()
            .encode(),
        )

    with httpx2.Client(transport=httpx2.MockTransport(peer)) as transport:
        client = AuthenticatedClient(
            base_url="https://control.invalid", token="fixture"
        ).set_httpx_client(transport)
        rejected = list_operations.sync_detailed(client=client, cursor=cursor, limit=20)
        assert rejected.status_code == 422 and rejected.parsed is not None
        restored = ProblemProducer.model_validate(rejected.parsed.to_dict())
        assert restored == problem
        assert observed[0].url.params["cursor"] == cursor
        assert observed[0].url.params["limit"] == "20"
        fresh = list_operations.sync_detailed(client=client, limit=20)
        assert fresh.status_code == 200 and fresh.parsed is not None
        assert fresh.parsed.to_dict()["operations"] == []
        assert fresh.parsed.to_dict()["total"] == 0
        assert fresh.parsed.to_dict()["next_cursor"] is None
        assert "cursor" not in observed[-1].url.params

"""The resume client reconnects to its exact accepted operator decision."""

import json

from vonk_agent_protocol import LifecycleState, OperatorActionName

from cluster_profiles import cli
from cluster_profiles.control_client import ControlTransportError

JOB = "11111111-1111-4111-8111-111111111111"
KEY = "22222222-2222-4222-8222-222222222222"


class LostResumeClient:
    request_timeout_seconds = 5.0

    def __init__(self, action=OperatorActionName.RESUME):
        self.calls = []
        self.action = action

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload, kwargs))
        if method == "POST":
            assert kwargs["extra_headers"]["X-Request-ID"] == KEY
            raise ControlTransportError("injected lost response")
        if path.endswith(f"/requests/{KEY}"):
            return {
                "job_id": JOB,
                "request_id": KEY,
                "actor": "operator",
                "action": self.action.value,
                "content_digest": "a" * 64,
            }
        assert path == f"/api/jobs/{JOB}"
        return {
            "id": JOB,
            "state": LifecycleState.RUNNING.value,
            "kind": "agent.operations",
            "authority_revision": "f" * 40,
            "targets": [],
            "target_total": 0,
            "current_attempt": 1,
            "operations": [],
            "operation_total": 0,
            "progress": None,
        }


def test_resume_lost_response_reads_exact_receipt_before_current_job(capsys):
    """Catches silently downgrading an unknown mutation to an unrelated job read."""
    client = LostResumeClient()
    result = cli.main(
        ["fleet", "resume", JOB, "--yes", "--request-key", KEY, "--json"],
        control_client=client,
    )
    captured = capsys.readouterr()
    assert result == 0, captured.err
    assert json.loads(captured.out)["id"] == JOB
    assert [(method, path) for method, path, *_ in client.calls] == [
        ("POST", f"/api/jobs/{JOB}/resume"),
        ("GET", f"/api/jobs/{JOB}/requests/{KEY}"),
        ("GET", f"/api/jobs/{JOB}"),
    ]


def test_resume_does_not_adopt_a_retirement_receipt(capsys):
    """Catches treating the same key's other accepted action as a resumed job."""
    client = LostResumeClient(OperatorActionName.RETIRE)
    result = cli.main(
        ["fleet", "resume", JOB, "--yes", "--request-key", KEY, "--json"],
        control_client=client,
    )
    capsys.readouterr()
    assert result != 0
    assert not any(path == f"/api/jobs/{JOB}" for _method, path, *_ in client.calls)

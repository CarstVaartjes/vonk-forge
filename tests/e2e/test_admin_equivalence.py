from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from urllib.request import Request

COMMIT = "a" * 40
DIGEST = "d" * 64
NODE_ID = "spk_0123456789abcdef0123456789abcdef"
RECONCILIATION_ID = "22222222-2222-4222-8222-222222222222"
JOB_ID = "11111111-1111-4111-8111-111111111111"
ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Job:
    id: str = JOB_ID
    state: str = "queued"
    kind: str = "reconcile"
    base_commit: str = COMMIT
    targets: tuple[str, ...] = (NODE_ID,)
    current_attempt: int = 1
    status_reason: str | None = None
    reconciliation_id: str | None = RECONCILIATION_ID


class Jobs:
    def __init__(self) -> None:
        self.job = Job()

    def enqueue(self, *_args: object, **_kwargs: object) -> Job:
        return self.job

    def get(self, job_id: str) -> Job:
        if job_id != self.job.id:
            raise KeyError(job_id)
        return self.job

    def list(self, *, limit: int = 100) -> list[Job]:
        del limit
        return [self.job]

    def list_page(self, **_kwargs):
        return [self.job], None, 1


class Repository:
    def head(self) -> str:
        return COMMIT


class Reconciler:
    def plan(
        self,
        commit: str,
        profile_id: str,
        *,
        fleet_evidence_digest: str,
    ) -> SimpleNamespace:
        del fleet_evidence_digest
        assert profile_id == "production"
        operation = {
            "compensation_kind": "start",
            "dependencies": [],
            "kind": "stop",
            "node_id": NODE_ID,
            "operation_id": f"stop:model-a:{NODE_ID}",
            "payload_digest": "e" * 64,
            "workload_id": "model-a",
        }
        return SimpleNamespace(
            agent_protocol_range=(3, 4),
            commit=commit,
            digest=DIGEST,
            input_digests={"fleet": "f" * 64},
            operation_graph=SimpleNamespace(
                reconciliation_id=RECONCILIATION_ID,
                document={
                    "base_commit": commit,
                    "nodes": [operation],
                    "schema_version": 1,
                    "targets": [NODE_ID],
                },
            ),
            placements={"model-a": (NODE_ID,)},
            releases={},
            routes={},
            targets=(NODE_ID,),
        )

    def enqueue(
        self,
        plan_digest: str,
        actor: str,
        request_id: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        del actor, request_id
        if plan_digest != DIGEST:
            raise ValueError("unknown reconciliation plan digest")
        return {
            "base_commit": COMMIT,
            "job_id": JOB_ID,
            "reconciliation_id": RECONCILIATION_ID,
            "state": "queued",
        }


def _fleet() -> dict[str, object]:
    return {
        "commit": COMMIT,
        "nodes": [
            {
                "agent_last_seen_at": None,
                "agent_online": False,
                "agent_state": "unavailable",
                "certificate_expires_at": None,
                "certificate_expiry_seconds": None,
                "compatibility": "incompatible",
                "disk_available_bytes": 0,
                "display_name": "Unavailable Compute",
                "healthy": False,
                "hostname": "unavailable.invalid",
                "id": NODE_ID,
                "labels": {},
                "last_seen_age_seconds": None,
                "last_seen_at": None,
                "lifecycle": "managed",
                "memory_available_bytes": 0,
                "probe_age_seconds": None,
                "profile": "production",
                "stale": True,
            }
        ],
    }


def _live_check(token_directory: Path) -> dict[str, object]:
    from fastapi.testclient import TestClient
    from vonk_control.api import AdminServices, create_app
    from vonk_control.audit import MemoryAuditStore
    from vonk_control.auth import Actor, TokenCodec

    from cluster_profiles.control_client import ControlClient

    codec = TokenCodec(b"k" * 32)
    token = codec.issue(Actor("operator", "operator"), ttl_seconds=100, now=0)
    api = TestClient(
        create_app(
            jobs=Jobs(),
            tokens=codec,
            audits=MemoryAuditStore(),
            fleet=_fleet,
            now=lambda: 10,
            admin=AdminServices(
                repository=Repository(),
                proposals=None,
                changes=None,
                reconciler=Reconciler(),
            ),
        )
    )
    headers = {"Authorization": f"Bearer {token}"}
    token_file = token_directory / "control.token"
    token_file.write_text(token + "\n")
    token_file.chmod(0o600)

    def opener(request: Request, timeout: float) -> object:
        del timeout
        response = api.request(
            request.method,
            request.selector,
            content=request.data,
            headers=dict(request.header_items()),
        )

        class LiveResponse:
            status = response.status_code
            headers = response.headers

            def read(self, size: int = -1) -> bytes:
                return response.content if size < 0 else response.content[:size]

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *args: object) -> None:
                del args

        return LiveResponse()

    web_response = api.post("/api/v1/profiles/production/plan", headers=headers)
    cli_plan = ControlClient(
        "https://control.invalid", token_file, opener=opener
    ).plan_profile("production").to_dict()
    assert web_response.status_code == 200
    web_plan = web_response.json()
    cli_authority = {
        "commit": cli_plan["commit"],
        "digest": cli_plan["digest"],
        "targets": cli_plan["targets"],
        "operations": cli_plan["operation_graph"]["nodes"],
    }
    web_authority = {
        "commit": web_plan["commit"],
        "digest": web_plan["digest"],
        "targets": web_plan["targets"],
        "operations": web_plan["operation_graph"]["nodes"],
    }
    assert cli_authority == web_authority

    stale = api.post(
        "/api/v1/reconciliations",
        headers=headers,
        json={
            "fleet_evidence_digest": web_plan["fleet_evidence_digest"],
            "plan_digest": "0" * 64,
        },
    )
    accepted = api.post(
        "/api/v1/reconciliations",
        headers=headers,
        json={
            "fleet_evidence_digest": web_plan["fleet_evidence_digest"],
            "plan_digest": web_plan["digest"],
        },
    )
    unavailable = api.get("/api/v1/fleet", headers=headers).json()["nodes"][0]
    assert stale.status_code == 409
    assert stale.json() == {"detail": "reconciliation plan digest is stale"}
    assert accepted.status_code == 202
    assert unavailable["healthy"] is False
    assert unavailable["stale"] is True
    assert unavailable["agent_online"] is False
    assert unavailable["compatibility"] == "incompatible"
    return {
        "accepted_job": accepted.json()["job_id"],
        "commit": web_plan["commit"],
        "digest": web_plan["digest"],
        "operation_count": len(web_plan["operation_graph"]["nodes"]),
        "stale_status": stale.status_code,
        "target_count": len(web_plan["targets"]),
        "unavailable_failed_closed": True,
    }


def test_cli_and_web_use_the_same_canonical_plan_and_fail_closed(
    tmp_path: Path,
) -> None:
    """One real API must give CLI and browser contracts identical plan authority."""
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(ROOT / "control"),
            "--frozen",
            "python",
            str(Path(__file__).resolve()),
            "--live-check",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "accepted_job": JOB_ID,
        "commit": COMMIT,
        "digest": DIGEST,
        "operation_count": 1,
        "stale_status": 409,
        "target_count": 1,
        "unavailable_failed_closed": True,
    }


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--live-check":
        raise SystemExit("expected --live-check TOKEN_DIRECTORY")
    print(json.dumps(_live_check(Path(sys.argv[2])), sort_keys=True))

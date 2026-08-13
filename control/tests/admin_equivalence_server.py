"""Disposable real HTTP control API for cross-generated-client tests."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.api import AdminServices, create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.browser_auth import BrowserAuthService
from vonk_control.models import Base, User
from vonk_control.passwords import hash_password

COMMIT = "a" * 40
PLAN_DIGEST = "d" * 64
NODE_ID = "spk_" + "1" * 32


class Jobs:
    def get(self, job_id: str) -> object:
        raise KeyError(job_id)

    def list_page(self, **_kwargs):
        return [], None, 0


class Repository:
    def head(self) -> str:
        return COMMIT

    def inspect(self, commit: str):
        return SimpleNamespace(commit=commit, documents={}, dependencies={})


class Reconciler:
    def plan(
        self,
        commit: str,
        profile_id: str,
        *,
        fleet_evidence_digest: str,
    ):
        del fleet_evidence_digest
        if profile_id != "production-agents":
            raise ValueError("profile is invalid")
        operation = {
            "operation_id": "model:node.probe",
            "node_id": NODE_ID,
            "workload_id": "model",
            "kind": "node.probe",
            "dependencies": [],
            "compensation_kind": None,
            "payload_digest": "9" * 64,
        }
        return SimpleNamespace(
            commit=commit,
            digest=PLAN_DIGEST,
            targets=(NODE_ID,),
            placements={"model": (NODE_ID,)},
            routes={},
            releases={},
            input_digests={"profile": "f" * 64},
            operation_graph=SimpleNamespace(
                reconciliation_id="22222222-2222-4222-8222-222222222222",
                document={
                    "base_commit": commit,
                    "nodes": [operation],
                    "schema_version": 1,
                    "targets": [NODE_ID],
                },
            ),
            agent_protocol_range=(3, 3),
        )

    def enqueue(
        self,
        digest: str,
        _actor: str,
        _request_id: str,
        **_kwargs: object,
    ):
        if digest != PLAN_DIGEST:
            raise ValueError("unknown plan")
        return {
            "base_commit": COMMIT,
            "job_id": "11111111-1111-4111-8111-111111111111",
            "reconciliation_id": "22222222-2222-4222-8222-222222222222",
            "state": "queued",
        }


def main() -> None:
    port = int(sys.argv[1])
    state_file = Path(sys.argv[2])
    ready_file = Path(sys.argv[3])
    codec = TokenCodec(b"k" * 32)
    token = codec.issue(Actor("operator", "operator"), ttl_seconds=3600, now=0)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    browser_password = "live-equivalence-browser-password"
    with sessions.begin() as db:
        db.add(
            User(
                subject="admin",
                role="administrator",
                disabled_at=None,
                password_verifier=hash_password(browser_password),
            )
        )
    browser_auth = BrowserAuthService(
        sessions,
        token_signing_key=b"k" * 32,
        clock=lambda: datetime.now(UTC),
    )
    browser_session = browser_auth.login("admin", browser_password)

    def fleet() -> dict[str, object]:
        available = json.loads(state_file.read_text())["available"]
        return {
            "commit": COMMIT,
            "nodes": [
                {
                    "agent_online": available,
                    "agent_state": "active" if available else "unavailable",
                    "compatibility": "supported" if available else "incompatible",
                    "disk_available_bytes": 2_000_000,
                    "display_name": "Compute A",
                    "healthy": available,
                    "hostname": "must-not-render.internal",
                    "id": NODE_ID,
                    "labels": {},
                    "lifecycle": "managed",
                    "memory_available_bytes": 1_000_000,
                    "profile": "production-agents",
                    "stale": not available,
                }
            ],
        }

    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet=fleet,
        now=lambda: 1,
        admin=AdminServices(
            repository=Repository(),
            proposals=None,
            changes=None,
            reconciler=Reconciler(),
        ),
        browser_auth=browser_auth,
    )
    ready_file.write_text(
        json.dumps(
            {
                "browser_csrf": browser_session.csrf,
                "browser_token": browser_session.token,
                "token": token,
            }
        )
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


if __name__ == "__main__":
    main()

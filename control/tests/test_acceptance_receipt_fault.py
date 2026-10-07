"""The disposable relay must lose a receipt before Controller ingress."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    ArtifactDistributionResult,
    OutcomeDone,
    OutcomeKind,
    RecipeStartResult,
    canonical_message,
)

ROOT = Path(__file__).resolve().parents[2]


def _relay(root: Path) -> tuple[ASGIApp, list[bytes]]:
    specification = importlib.util.spec_from_file_location(
        "acceptance_receipt_fault", ROOT / "tests/acceptance/receipt_fault.py"
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    accepted: list[bytes] = []

    async def controller(_scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        accepted.append(message.get("body", b""))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return module.LostStartReceipt(controller, root), accepted


def _post(relay: ASGIApp, document: AgentResult) -> list[int]:
    responses: list[int] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": canonical_message(document)}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            responses.append(message["status"])

    asyncio.run(
        relay(
            {"type": "http", "method": "POST", "path": "/agent/result"},
            receive,
            send,
        )
    )
    return responses


def test_start_receipt_is_lost_before_controller_then_exact_retry_is_accepted(
    tmp_path: Path,
) -> None:
    (tmp_path / "armed.json").write_text("{}")
    relay, accepted = _relay(tmp_path)
    receipt = AgentResult(
        state=AgentResultState.SUCCEEDED,
        fence="11111111-1111-4111-8111-111111111111",
        result=OutcomeDone(
            kind=OutcomeKind.DONE,
            result=RecipeStartResult(endpoint="http://172.31.44.1:8000"),
        ),
    )
    assert _post(relay, receipt)[0] == 503
    assert _post(relay, receipt)[0] == 503
    assert accepted == []
    assert not (tmp_path / "replayed.json").exists()
    replay = receipt.model_copy(
        update={"fence": "22222222-2222-4222-8222-222222222222"}
    )
    assert _post(relay, replay)[0] == 503
    assert accepted == []
    (tmp_path / "recovered.json").write_text("{}")
    assert _post(relay, receipt)[0] == 503
    assert accepted == []
    assert json.loads((tmp_path / "last-start-receipt.json").read_text()) == {
        "fence": receipt.fence,
        "gate": "old-fence",
    }
    assert _post(relay, replay)[0] == 204
    assert json.loads((tmp_path / "last-start-receipt.json").read_text()) == {
        "fence": replay.fence,
        "gate": "released",
    }
    assert len(accepted) == 1
    assert json.loads((tmp_path / "replayed.json").read_text()) == {
        "fence": replay.fence,
        "endpoint": "http://172.31.44.1:8000",
    }


def test_non_start_receipt_is_not_interrupted(tmp_path: Path) -> None:
    (tmp_path / "armed.json").write_text("{}")
    relay, accepted = _relay(tmp_path)
    receipt = AgentResult(
        fence="11111111-1111-4111-8111-111111111111",
        state=AgentResultState.SUCCEEDED,
        result=OutcomeDone(
            kind=OutcomeKind.DONE,
            result=ArtifactDistributionResult(downloaded_bytes=12),
        ),
    )
    assert _post(relay, receipt)[0] == 204
    assert len(accepted) == 1
    assert not (tmp_path / "blocked.json").exists()


def test_unarmed_receipt_is_not_interrupted(tmp_path: Path) -> None:
    relay, accepted = _relay(tmp_path)
    receipt = AgentResult(
        fence="11111111-1111-4111-8111-111111111111",
        state=AgentResultState.SUCCEEDED,
        result=OutcomeDone(
            kind=OutcomeKind.DONE,
            result=RecipeStartResult(endpoint="http://172.31.44.1:8000"),
        ),
    )
    assert _post(relay, receipt)[0] == 204
    assert len(accepted) == 1
    assert not (tmp_path / "blocked.json").exists()


def test_disposable_journal_fault_invalidates_wal_and_preserves_managed_work(
    tmp_path: Path,
) -> None:
    """A main-only fault leaves the pending receipt recoverable from the WAL."""
    receipt = AgentResult(
        state=AgentResultState.SUCCEEDED,
        fence="11111111-1111-4111-8111-111111111111",
        result=OutcomeDone(
            kind=OutcomeKind.DONE,
            result=RecipeStartResult(endpoint="http://172.31.44.1:8000"),
        ),
    )
    writer = (
        "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
        "c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=FULL'); "
        "c.execute('CREATE TABLE acceptance_pending_receipt (body BLOB NOT NULL)'); "
        "c.execute('INSERT INTO acceptance_pending_receipt VALUES (?)',(sys.stdin.buffer.read(),)); "
        "c.commit(); os._exit(0)"
    )
    for root_name in ("old-main-only", "complete-journal-fault"):
        root = tmp_path / root_name
        root.mkdir()
        journal = root / "state.sqlite"
        managed = root / "managed-model.bin"
        managed.write_bytes(b"retained verified artifact bytes")
        credential = root / "credential-fixture"
        credential.write_bytes(b"retained fixture identity")
        subprocess.run(
            [sys.executable, "-c", writer, str(journal)],
            input=canonical_message(receipt),
            check=True,
            timeout=2,
        )
        assert Path(str(journal) + "-wal").is_file()
        if root_name == "old-main-only":
            journal.write_bytes(b"acceptance-lost-start-journal")
            with sqlite3.connect(journal) as reader:
                stored = reader.execute(
                    "SELECT body FROM acceptance_pending_receipt"
                ).fetchone()
            assert stored is not None
            assert AgentResult.model_validate_json(stored[0], strict=True) == receipt
        else:
            fault = json.loads(
                subprocess.check_output(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import json,runpy,sys; from pathlib import Path; "
                            "m=runpy.run_path('tests/acceptance/test_spark_lifecycle.py'); "
                            "print(json.dumps(m['_agent_journal_fault_command'](Path(sys.argv[1]))))"
                        ),
                        str(journal),
                    ],
                    cwd=ROOT,
                    timeout=2,
                )
            )
            result = subprocess.run(
                [sys.executable, *fault[2:]], capture_output=True, check=True, timeout=2
            )
            observation = json.loads(result.stdout)
            assert observation == {
                "wal_before": True,
                "shm_before": True,
                "wal_after": False,
                "shm_after": False,
            }
            with sqlite3.connect(journal) as reader:
                try:
                    reader.execute(
                        "SELECT body FROM acceptance_pending_receipt"
                    ).fetchone()
                except sqlite3.DatabaseError:
                    pass
                else:
                    raise AssertionError(
                        "faulted journal unexpectedly retained its pending receipt"
                    )
        assert managed.read_bytes() == b"retained verified artifact bytes"
        assert credential.read_bytes() == b"retained fixture identity"

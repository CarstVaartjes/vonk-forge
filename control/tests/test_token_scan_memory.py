"""Collectors scan persisted JSON documents one batch at a time, not whole tables."""

from __future__ import annotations

import ast
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.catalog_revision_collection import operation_tokens
from vonk_control.models import Base, Job

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)
DOCUMENTS = 150
DOCUMENT_BYTES = 60_000  # a live job payload may be up to 64 KiB


def test_operation_token_scan_memory_does_not_grow_with_the_table(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'scan.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    named = "a" * 64
    with sessions.begin() as session:
        session.add_all(
            Job(
                request_id=f"00000000-0000-4000-8000-{index:012d}",
                kind="probe",
                state="queued",
                actor="operator",
                authority_revision="b" * 40,
                targets=[],
                payload_digest="c" * 64,
                payload={"digest": named, "filler": f"{index}-" + "x" * DOCUMENT_BYTES},
                current_attempt=0,
                created_at=NOW,
                updated_at=NOW,
            )
            for index in range(DOCUMENTS)
        )

    with sessions() as session:
        tracemalloc.start()
        try:
            found = operation_tokens(session, NOW)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    assert named in found
    table_bytes = DOCUMENTS * DOCUMENT_BYTES
    # Buffering every row costs the whole table about twice over (decoded
    # documents and their JSON text); a batch costs a fraction of it.
    assert peak < table_bytes, (peak, table_bytes)


def test_collectors_never_feed_a_buffered_result_to_the_token_scan() -> None:
    """Every call to ``tokens`` takes a stream, never ``session.scalars(...)``."""

    source = Path(__file__).parents[1] / "src" / "vonk_control"
    offenders: list[str] = []
    for path in sorted(source.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "tokens(" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tokens"
                and any(
                    isinstance(argument, ast.Call)
                    and isinstance(argument.func, ast.Attribute)
                    and argument.func.attr in {"scalars", "execute"}
                    for argument in node.args
                )
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders

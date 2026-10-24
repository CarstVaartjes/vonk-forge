"""The Library's local-state read: latest cache request per digest, at linear cost.

The read once joined every requested digest against the whole cache-operation
history (and every artifact set) with OR'd JSON-path and ``LIKE`` conditions,
so a page of a few hundred digests cost seconds per digest on PostgreSQL. These
tests pin the behaviour of that read against the old statement shape, and bound
the work PostgreSQL does by the rows involved rather than by their product.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import String, column, create_engine, event, func, or_, select, values
from sqlalchemy import cast as sql_cast
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import OperationProgress
from vonk_control.auth import TokenCodec
from vonk_control.library_contract import LibraryLocalState
from vonk_control.library_projection import LibraryProjection, local_state
from vonk_control.model_cache_contract import (
    CacheManifest,
    CacheOperationKind,
    ModelCacheOperationProgress,
)
from vonk_control.models import Base, ModelCacheOperation, ModelCacheSet
from vonk_control.stored_json import write_guard_mode
from vonk_control.strict_json import serialize_json_value
from vonk_forge_contracts.model import ModelReference

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
_KINDS = ("download", "download", "repair", "remove")
_STATES = ("succeeded", "failed", "running", "queued", "cancelled", "backoff")


def _digest(name: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_OID, name).hex * 2


def _manifest(model: str, recipe: str | None, verified: Iterable[str]) -> object:
    return serialize_json_value(
        CacheManifest(
            schema_version=2,
            source_policy="nas-first",
            model_content_sha256=model,
            recipe_revision_sha256=recipe,
            model_definition_ref=None,
            model_content_digests=[model],
            artifacts=[],
            verified_models=[
                ModelReference(
                    publisher="test", slug=f"model-{index}", content_sha256=digest
                )
                for index, digest in enumerate(verified)
            ],
        )
    )


def _progress(completed: int) -> object:
    return serialize_json_value(
        ModelCacheOperationProgress(
            phase="downloading",
            completed_artifacts=0,
            total_artifacts=1,
            downloaded_bytes=completed,
            expected_bytes=100,
            total_bytes_known=True,
            measurement=OperationProgress(
                phase="downloading",
                completed_bytes=completed,
                total_bytes=100,
                total_bytes_known=True,
                completed_items=0,
                total_items=1,
            ),
        )
    )


def _cache_set(
    set_id: str, model: str, recipe: str | None, state: str, manifest: object
) -> ModelCacheSet:
    return ModelCacheSet(
        artifact_set_sha256=set_id,
        schema_version=2,
        model_content_sha256=model,
        recipe_revision_sha256=recipe,
        manifest=manifest,
        expected_bytes=100,
        verified_bytes=0,
        state=state,
        protected=False,
        protected_reasons=[],
        created_at=NOW,
        updated_at=NOW,
        last_accessed_at=NOW,
    )


def _operation(
    set_id: str | None, *, kind: str, state: str, at: datetime, completed: int
) -> ModelCacheOperation:
    # Download and repair payloads carry their identity in the artifact set
    # (ModelCacheDownloadPayload has no top-level digests); ``{}`` is the
    # fixture placeholder the write guard accepts.
    return ModelCacheOperation(
        id=str(uuid.uuid4()),
        request_key=str(uuid.uuid4()),
        kind=kind,
        state=state,
        artifact_set_sha256=set_id,
        payload={},
        progress=_progress(completed),
        actor="test",
        created_at=at,
        updated_at=at,
    )


def _seed_varied(sessions: sessionmaker[Session]) -> dict[str, str]:
    """Sets reached by model, recipe and verified alias; ties; removals; orphans."""

    d = {name: _digest(name) for name in ("m1", "m2", "m3", "m4", "m5", "m6")}
    d |= {name: _digest(name) for name in ("r1", "r2", "a1", "n1", "absent")}
    sets = [
        # (set, model, recipe, set state, verified models)
        ("s1", d["m1"], None, "cached", [d["m1"], d["a1"]]),
        ("s2", d["m2"], d["r1"], "downloading", [d["m2"]]),
        ("s3", d["m3"], None, "failed", []),
        ("s4", d["n1"], d["r2"], "incomplete", [d["n1"]]),
        ("s5", d["n1"], None, "cached", [d["n1"], d["m4"]]),
        ("s6", d["m5"], None, "cached", [d["m5"]]),
        ("s7", d["m6"], None, "needs-repair", [d["m6"], d["m1"]]),
    ]
    rng = random.Random(1894)
    instants = [NOW - timedelta(minutes=minutes) for minutes in (0, 5, 5, 9, 30)]
    with sessions.begin() as session:
        for name, model, recipe, state, verified in sets:
            session.add(
                _cache_set(
                    _digest(name),
                    model,
                    recipe,
                    state,
                    _manifest(model, recipe, verified),
                )
            )
        session.flush()
        for name, *_ in sets:
            for index in range(8):
                kind = "remove" if name == "s6" else rng.choice(_KINDS)
                session.add(
                    _operation(
                        _digest(name),
                        kind=kind,
                        state=rng.choice(_STATES),
                        # Equal request times leave the order to the UUID tie-break.
                        at=rng.choice(instants),
                        completed=index,
                    )
                )
        session.add(
            _operation(None, kind="download", state="running", at=NOW, completed=1)
        )
    return d


def _old_verifies_requested_model(_session: Session, digests: set[str]):
    """The set prefilter before this fix: a LIKE per (set, requested digest)."""

    requested = (
        values(column("digest", String), name="requested")
        .data([(digest,) for digest in sorted(digests)])
        .cte()
    )
    verified = ModelCacheSet.manifest["verified_models"].as_json()
    reference_match = sql_cast(verified, String).contains(requested.c.digest)
    return select(requested.c.digest).where(reference_match).exists()


def _old_latest_cache_operations(digests: set[str]):
    """The history selection before this fix: (operation x digest) OR-join."""

    requested = (
        values(column("digest", String), name="requested")
        .data([(digest,) for digest in sorted(digests)])
        .cte()
    )
    verified = ModelCacheSet.manifest["verified_models"].as_json()
    reference_match = sql_cast(verified, String).contains(requested.c.digest)
    payload = ModelCacheOperation.payload
    latest = (
        select(
            ModelCacheOperation.id,
            func.row_number()
            .over(
                partition_by=requested.c.digest,
                order_by=(
                    ModelCacheOperation.created_at.desc(),
                    ModelCacheOperation.id.desc(),
                ),
            )
            .label("position"),
        )
        .join(
            requested,
            or_(
                payload["model_content_sha256"].as_string() == requested.c.digest,
                payload["recipe_revision_sha256"].as_string() == requested.c.digest,
                select(ModelCacheSet.artifact_set_sha256)
                .where(
                    ModelCacheSet.artifact_set_sha256
                    == ModelCacheOperation.artifact_set_sha256,
                    or_(
                        ModelCacheSet.model_content_sha256 == requested.c.digest,
                        ModelCacheSet.recipe_revision_sha256 == requested.c.digest,
                        reference_match,
                    ),
                )
                .exists(),
            ),
        )
        .where(
            ModelCacheOperation.kind.in_(
                (CacheOperationKind.DOWNLOAD, CacheOperationKind.REPAIR)
            )
        )
        .subquery()
    )
    return select(latest.c.id).where(latest.c.position == 1)


def _projection(sessions: sessionmaker[Session]) -> LibraryProjection:
    return LibraryProjection(
        sessions, cursors=TokenCodec(b"q" * 32).cursor_codec(), clock=lambda: NOW
    )


def _read(
    projection: LibraryProjection, digests: list[str]
) -> dict[str, LibraryLocalState]:
    return {
        digest: state
        for digest, state in projection._database_local_state(digests).items()
        if digest in digests
    }


def _assert_old_and_new_agree(engine: Engine, monkeypatch) -> None:
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    d = _seed_varied(sessions)
    projection = _projection(sessions)
    scopes = [
        list(d.values()),
        [d["m1"]],
        [d["a1"]],
        [d["m4"]],
        [d["r1"], d["r2"]],
        [d["m5"], d["absent"]],
        [d["m1"], d["m6"], d["a1"]],
    ]
    for digests in scopes:
        new = _read(projection, digests)
        with monkeypatch.context() as old:
            old.setattr(
                local_state,
                "_verifies_requested_model",
                _old_verifies_requested_model,
            )
            old.setattr(
                local_state,
                "_latest_cache_operations",
                lambda _members, requested=set(digests): _old_latest_cache_operations(
                    requested
                ),
            )
            before = _read(projection, digests)
        assert new == before, digests
    every = _read(projection, list(d.values()))
    # The fixture reaches each path: a verified alias, a recipe, ties.
    assert every[d["a1"]].preparation is not None
    assert every[d["m4"]].preparation is not None
    assert every[d["r1"]].preparation is not None
    # Removals are not download history; an absent digest has no state.
    assert every[d["m5"]].preparation is None
    assert d["absent"] not in every


def test_latest_request_per_digest_matches_the_old_statement_on_sqlite(
    tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'local-state.sqlite'}")
    _assert_old_and_new_agree(engine, monkeypatch)


def test_latest_request_per_digest_matches_the_old_statement_on_postgres(
    postgres_engine, monkeypatch
):
    _assert_old_and_new_agree(postgres_engine, monkeypatch)


def test_damaged_manifest_text_does_not_hide_a_digest_latest_request(tmp_path):
    """A digest named only by a damaged manifest no longer masks its real history.

    The old text match ranked the damaged set's newer request as the digest's
    latest and then never credited it, so the digest lost its own progress.
    """

    engine = create_engine(f"sqlite:///{tmp_path / 'damaged.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    wanted, other = _digest("wanted"), _digest("other")
    with sessions.begin() as session:
        session.add(
            _cache_set(
                _digest("good"),
                wanted,
                None,
                "downloading",
                _manifest(wanted, None, [wanted]),
            )
        )
        with write_guard_mode(strict=False):
            session.add(
                _cache_set(
                    _digest("damaged"),
                    other,
                    None,
                    "downloading",
                    {
                        "schema_version": 2,
                        "verified_models": [
                            {
                                "kind": "model",
                                "publisher": "test",
                                "slug": "model-0",
                                "content_sha256": wanted,
                            }
                        ],
                    },
                )
            )
            session.flush()
        good = _operation(
            _digest("good"),
            kind="download",
            state="running",
            at=NOW - timedelta(minutes=1),
            completed=7,
        )
        session.add_all(
            [
                good,
                _operation(
                    _digest("damaged"),
                    kind="download",
                    state="running",
                    at=NOW,
                    completed=9,
                ),
            ]
        )
        session.flush()
        good_id = good.id
    local = _projection(sessions)._database_local_state([wanted])[wanted]
    assert local.preparation is not None
    assert local.preparation.operation_id == good_id


def _plan_work(node: dict[str, object]) -> int:
    """Rows produced plus filter evaluations, over every plan node."""

    loops = node.get("Actual Loops", 0)
    rows = node.get("Actual Rows", 0)
    assert isinstance(loops, int | float) and isinstance(rows, int | float)
    work = int(loops + rows * loops)
    for key in ("Rows Removed by Filter", "Rows Removed by Join Filter"):
        removed = node.get(key, 0)
        assert isinstance(removed, int | float)
        work += int(removed * max(loops, 1))
    children = node.get("Plans", [])
    assert isinstance(children, list)
    return work + sum(_plan_work(child) for child in children)


def test_local_state_read_work_is_linear_in_history_sets_and_digests(
    postgres_engine,
):
    """PostgreSQL work grows with history + sets + digests, not their product.

    Fails on the (operation x digest) OR-join and the (set x digest) LIKE:
    either alone does at least digests x rows of work here.
    """

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    digests = [_digest(f"model-{index}") for index in range(64)]
    history = 640
    rng = random.Random(7)
    with sessions.begin() as session:
        for index, digest in enumerate(digests):
            # Half the sets are reached only through a verified alias.
            model = digest if index % 2 else _digest(f"unrequested-{index}")
            session.add(
                _cache_set(
                    _digest(f"set-{index}"),
                    model,
                    None,
                    "cached",
                    _manifest(model, None, [model, digest]),
                )
            )
        session.flush()
        for index in range(history):
            session.add(
                _operation(
                    _digest(f"set-{rng.randrange(len(digests))}"),
                    kind="download",
                    state="succeeded",
                    at=NOW - timedelta(seconds=index),
                    completed=1,
                )
            )
    statements: list[tuple[str, object]] = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    event.listen(postgres_engine, "before_cursor_execute", capture)
    try:
        local = _projection(sessions)._database_local_state(digests)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", capture)
    assert all(local[digest].preparation is not None for digest in digests)
    work = 0
    raw = postgres_engine.raw_connection()
    try:
        for statement, parameters in statements:
            cursor = raw.cursor()
            cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + statement, parameters)
            (plan,) = cursor.fetchone()
            work += _plan_work(plan[0]["Plan"])
    finally:
        raw.close()
    rows = history + 2 * len(digests)
    # A product shape does >= 64 x 640 = 40,960 evaluations on its own.
    assert work < 12 * rows, work

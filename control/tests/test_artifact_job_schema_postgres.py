"""The startup reconcile moves an artifact-job table onto the preparation shape.

A database written before the rename has a NOT NULL ``state`` whose CHECK knows
``draft``, ``ready``, ``cancelling`` and ``waiting-for-operator``, and neither the
``preparation`` nor the ``cancel_requested_at`` column.  Startup adds the columns,
lets ``state`` be ``NULL`` and rebuilds the CHECK so that both spellings are
admitted for one release.
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from vonk_control.db import reconcile_schema
from vonk_control.models import ArtifactJob, Base

TABLE = ArtifactJob.__tablename__
OLD_CHECK = (
    "state IN ('draft','ready','queued','running','cancelling',"
    "'waiting-for-operator','succeeded','failed','cancelled')"
)


def _admits(connection, condition: str) -> bool:
    return bool(
        connection.execute(
            text(f"SELECT {condition}"),
        ).scalar()
    )


def test_an_old_artifact_job_table_is_reconciled_in_place(postgres_engine):
    with postgres_engine.begin() as connection:
        Base.metadata.create_all(connection)
        connection.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN preparation"))
        connection.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN cancel_requested_at"))
        connection.execute(text(f"ALTER TABLE {TABLE} ALTER COLUMN state SET NOT NULL"))
        connection.execute(
            text(f"ALTER TABLE {TABLE} DROP CONSTRAINT ck_artifact_jobs_state")
        )
        connection.execute(
            text(
                f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_artifact_jobs_state "
                f"CHECK ({OLD_CHECK})"
            )
        )

    with postgres_engine.begin() as connection:
        reconcile_schema(connection)

    with postgres_engine.connect() as connection:
        columns = {
            item["name"]: item for item in inspect(connection).get_columns(TABLE)
        }
        assert "preparation" in columns and "cancel_requested_at" in columns
        assert columns["state"]["nullable"] is True
        check = next(
            item["sqltext"]
            for item in inspect(connection).get_check_constraints(TABLE)
            if item["name"] == "ck_artifact_jobs_state"
        )
        for word in ("queued", "observing", "needs-operator", "cancelling", "draft"):
            assert f"'{word}'" in check, word
        assert "IS NULL" in check.upper()

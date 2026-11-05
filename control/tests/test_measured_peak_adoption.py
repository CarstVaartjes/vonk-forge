"""Startup folds the peaks older versions kept on rows into the measured table."""

from datetime import timedelta

from sqlalchemy import select, text, update
from vonk_control.measured_peak_adoption import adopt_measured_role_peaks
from vonk_control.models import CatalogDocumentRevision, MeasuredRolePeak, RunNode
from vonk_control.observed_run_memory import observed_role_peaks

from .test_load_phase_swap_admission import _accept, _inventory, _swap, _world


def test_stored_row_peaks_become_the_measured_peaks_once(
    tmp_path, postgres_engine
) -> None:
    """Catches a deploy forgetting what runs and installations already measured.

    The run keeps 70 and an older schema's installation column keeps 90: the
    table takes the higher, a second pass changes nothing, and a peak a newer
    Controller recorded above both is never lowered.
    """
    sessions, now, node, installation, service = _world(tmp_path, postgres_engine)
    at = now + timedelta(seconds=1)
    _inventory(sessions, at, node, swap=_swap(64))
    run_id = _accept(
        sessions, service, service.plan_run(installation, alias="a", now=at), at
    )
    with sessions.begin() as session:
        session.execute(
            update(RunNode)
            .where(RunNode.run_id == run_id)
            .values(observed_memory_bytes=70)
        )
        role = session.scalars(select(RunNode.role)).one()
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE installation_nodes "
                "ADD COLUMN IF NOT EXISTS observed_memory_bytes BIGINT"
            )
        )
        connection.execute(
            text("UPDATE installation_nodes SET observed_memory_bytes = 90")
        )
        assert adopt_measured_role_peaks(connection) == 1
        assert adopt_measured_role_peaks(connection) == 0
    with sessions() as session:
        digest = session.scalars(
            select(CatalogDocumentRevision.content_digest).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        ).one()
        assert observed_role_peaks(session, digest) == {role: 90}
    with sessions.begin() as session:
        row = session.get(MeasuredRolePeak, (digest, role))
        assert row is not None
        row.peak_bytes = 120
    with postgres_engine.begin() as connection:
        assert adopt_measured_role_peaks(connection) == 0
    with sessions() as session:
        assert observed_role_peaks(session, digest) == {role: 120}

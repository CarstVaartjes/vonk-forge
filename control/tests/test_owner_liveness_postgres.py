"""Work that used to read a stored state as "its owner is working" asks the owner.

A build row in ``building`` pins its image only while a live Job owns it; a
renewal intent another CSR meets is replaced at once once no renewal touches
it, and kept while one does.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState
from vonk_control.enrollment import EnrollmentService
from vonk_control.image_store_collection import ImageStoreCollector
from vonk_control.models import AgentCertificateRotation, Base, Job, RecipeBuild
from vonk_control.owner_liveness import (
    ISSUANCE_WINDOW,
    OWNER_SETTLE_GRACE,
    build_in_flight,
    builds_in_flight,
)
from vonk_control.pki import IssuedCertificate

from .test_build_cancellation_recovery import _services
from .test_enrollment import NODE_ID, Clock, RecordingAuthority, csr, enroll

_LAYOUT = "a" * 64


@pytest.mark.parametrize("ending", ["live", LifecycleState.FAILED.value, "missing"])
def test_a_building_row_pins_its_image_only_while_its_job_is_live(
    tmp_path, postgres_engine, ending
) -> None:
    sessions, _builds, operations, _storage, now, *_rest, plan = _services(
        tmp_path, postgres_engine
    )
    held = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    with sessions.begin() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        build.oci_layout_sha256 = _LAYOUT
        job = session.get(Job, held.id)
        assert job is not None
        if ending == "missing":
            job.payload = {**job.payload, "owner_id": str(uuid.uuid4())}
        elif ending != "live":
            job.state = ending
    later = now + OWNER_SETTLE_GRACE + timedelta(seconds=1)

    with sessions() as session:
        waiting = ImageStoreCollector._waiting_builds(session, later)
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        in_flight = build_in_flight(session, build, later)
        selected = session.scalar(
            select(RecipeBuild.id).where(
                RecipeBuild.id == build.id, builds_in_flight(later)
            )
        )

    assert (_LAYOUT in waiting) is (ending == "live")
    # The row filter and the owner arm are one rule.
    assert in_flight is (ending == "live")
    assert (selected is not None) is in_flight


@pytest.mark.parametrize("touched", [False, True])
def test_another_csr_replaces_a_renewal_intent_only_once_its_owner_ended(
    postgres_engine, touched
) -> None:
    """``touched``: a renewal touched the intent within the issuance window."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    clock = Clock()
    authority = RecordingAuthority()
    service = EnrollmentService(sessions, authority, clock=clock)
    source = enroll(service)
    request = csr()
    # A renewal of this key left its intent issuing, bound to other CSR bytes.
    authority.renew_error = SystemExit("process death after the provider request")
    with pytest.raises(SystemExit):
        service.renew(NODE_ID, source.serial, request)
    authority.renew_error = None
    with sessions.begin() as session:
        intent = session.get(AgentCertificateRotation, NODE_ID)
        assert intent is not None
        intent.csr_pem = intent.csr_pem.replace("\n", "\r\n")
    if touched:
        clock.advance(seconds=int(ISSUANCE_WINDOW.total_seconds()) - 60)
    else:
        clock.advance(seconds=int(ISSUANCE_WINDOW.total_seconds()) + 1)

    renewed = service.renew(NODE_ID, source.serial, request)

    # Without a sweep in between: an ended intent is replaced on the spot.
    assert isinstance(renewed, IssuedCertificate) is not touched
    with sessions() as session:
        assert (session.get(AgentCertificateRotation, NODE_ID) is not None) is touched

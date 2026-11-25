"""One model name, one owner: the namespace lock decides under real PostgreSQL."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from vonk_control.admission_locking import AdmissionLockBusy
from vonk_control.gateway.alias_namespace import (
    AliasOwner,
    AliasTaken,
    alias_owner,
    claim_external_alias,
    lock_alias_namespace,
    require_alias_not_external,
)
from vonk_control.gateway.external_contract import (
    ExternalDeploymentSpec,
    ExternalDestination,
)
from vonk_control.model_primitives import Base
from vonk_control.models.gateway import GatewayExternalDeployment

ALIAS = "partner-large"
WAIT_SECONDS = 30


def _deployment() -> GatewayExternalDeployment:
    now = datetime.now(UTC)
    return GatewayExternalDeployment(
        alias=ALIAS,
        desired_revision=1,
        desired_spec=ExternalDeploymentSpec(
            alias=ALIAS,
            litellm_params={"model": "openai/partner-large"},
            destination=ExternalDestination(origins=["provider:openai"]),
        ),
        request_id=str(uuid4()),
        created_at=now,
        updated_at=now,
    )


def test_the_loser_of_a_race_for_one_name_is_refused_and_nothing_is_repointed(
    postgres_engine,
):
    # Break caught: a check without the namespace lock lets an external claim and
    # a local admission both pass, so one name serves two owners.
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        lock_alias_namespace(session)  # the singleton exists before the race
    claimed = threading.Event()
    release = threading.Event()

    def external_claim() -> None:
        with sessions.begin() as session:
            claim_external_alias(session, ALIAS)
            session.add(_deployment())
            session.flush()
            claimed.set()
            assert release.wait(WAIT_SECONDS)

    with ThreadPoolExecutor(max_workers=1) as pool:
        winner = pool.submit(external_claim)
        assert claimed.wait(WAIT_SECONDS)
        with sessions.begin() as session, pytest.raises(AdmissionLockBusy):
            lock_alias_namespace(session)
        release.set()
        winner.result(timeout=WAIT_SECONDS)

    with sessions.begin() as session:
        lock_alias_namespace(session)
        assert alias_owner(session, ALIAS) is AliasOwner.EXTERNAL
        with pytest.raises(AliasTaken):
            require_alias_not_external(session, {ALIAS})
        with pytest.raises(AliasTaken):
            claim_external_alias(session, ALIAS)

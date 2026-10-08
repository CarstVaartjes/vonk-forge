"""The public service keeps its bound retry behavior after method extraction."""

from typing import cast

import pytest
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import RecipeBuildCode
from vonk_control.recipe_builds import RecipeBuildService, RecipeBuildUnknown
from vonk_control.source_bundles import SourceBundleStoreProtocol
from vonk_control.source_policy import SourcePolicyReport


def test_extracted_source_retry_is_bounded_and_a_fresh_request_succeeds(monkeypatch):
    """Catches a lost descriptor receiver or a retry outcome that poisons later reads."""
    report = SourcePolicyReport(True, "a" * 64, "Dockerfile", ())
    fault = RecipeBuildUnknown(RecipeBuildCode.SOURCE_UNAVAILABLE.value, "unavailable")
    attempts: list[str] = []
    delays: list[float] = []
    unavailable = [True]

    def observe(self: RecipeBuildService, revision: str) -> SourcePolicyReport:
        attempts.append(revision)
        if unavailable[0]:
            raise fault
        return report

    monkeypatch.setattr(RecipeBuildService, "_check_source_once", observe)
    service = RecipeBuildService(
        cast(sessionmaker[Session], None),
        bundles=cast(SourceBundleStoreProtocol, None),
        sleep=delays.append,
    )
    with pytest.raises(RecipeBuildUnknown) as exhausted:
        service.check_source("revision")
    assert exhausted.value is fault
    assert len(attempts) == 3
    assert sum(delays) < 1
    unavailable[0] = False
    assert service.check_source("fresh-revision") is report
    assert attempts[-1] == "fresh-revision"

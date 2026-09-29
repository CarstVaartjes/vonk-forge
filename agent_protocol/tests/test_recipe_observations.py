from __future__ import annotations

import pytest
from vonk_agent_protocol import AgentProtocolError, RecipeRunObservationsWire

RUN = {
    "run_id": "10000000-0000-4000-8000-000000000001",
    "run_generation": 3,
    "process_running": True,
    "endpoint_ready": None,
}
REPORT = {"observed_at": "2026-09-07T00:00:00+02:00", "runs": [RUN]}


def test_observation_report_round_trips_and_normalizes_time_to_utc() -> None:
    report = RecipeRunObservationsWire.parse(REPORT)
    assert report.observed_at.isoformat() == "2026-09-06T22:00:00+00:00"
    assert report.runs[0].process_running is True
    assert report.runs[0].endpoint_ready is None


@pytest.mark.parametrize(
    "report",
    [
        REPORT | {"observed_at": "2026-09-07T00:00:00"},
        REPORT | {"runs": [RUN | {"process_running": 1}]},
        REPORT | {"runs": [RUN | {"run_generation": 0}]},
        REPORT | {"runs": [RUN | {"helper_receipt": {}}]},
        REPORT | {"runs": [RUN, RUN]},
    ],
)
def test_observation_report_rejects_invalid_reports(report: dict[str, object]) -> None:
    with pytest.raises(AgentProtocolError):
        RecipeRunObservationsWire.parse(report)

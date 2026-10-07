from typing import Literal

FleetFrameIssueReasonCode = Literal['fleet.frame_budget_exceeded', 'fleet.frame_encoding_unavailable', 'fleet.stored_event_payload_unavailable']

FLEET_FRAME_ISSUE_REASON_CODE_VALUES: set[FleetFrameIssueReasonCode] = { 'fleet.frame_budget_exceeded', 'fleet.frame_encoding_unavailable', 'fleet.stored_event_payload_unavailable',  }

def check_fleet_frame_issue_reason_code(value: str) -> FleetFrameIssueReasonCode:
    if value in FLEET_FRAME_ISSUE_REASON_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_FRAME_ISSUE_REASON_CODE_VALUES!r}")

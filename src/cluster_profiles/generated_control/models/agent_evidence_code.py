from typing import Literal

AgentEvidenceCode = Literal['agent_evidence.claim_hint_dropped', 'agent_evidence.failure_diagnostics_dropped', 'agent_evidence.inventory_fabric_dropped', 'agent_evidence.inventory_nas_route_dropped', 'agent_evidence.inventory_network_dropped', 'agent_evidence.inventory_network_interface_dropped', 'agent_evidence.progress_dropped', 'agent_evidence.telemetry_reading_dropped']

AGENT_EVIDENCE_CODE_VALUES: set[AgentEvidenceCode] = { 'agent_evidence.claim_hint_dropped', 'agent_evidence.failure_diagnostics_dropped', 'agent_evidence.inventory_fabric_dropped', 'agent_evidence.inventory_nas_route_dropped', 'agent_evidence.inventory_network_dropped', 'agent_evidence.inventory_network_interface_dropped', 'agent_evidence.progress_dropped', 'agent_evidence.telemetry_reading_dropped',  }

def check_agent_evidence_code(value: str) -> AgentEvidenceCode:
    if value in AGENT_EVIDENCE_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {AGENT_EVIDENCE_CODE_VALUES!r}")

from typing import Literal, cast

AgentDeploymentEvidenceConnectivity = Literal['offline', 'recent', 'unknown']

AGENT_DEPLOYMENT_EVIDENCE_CONNECTIVITY_VALUES: set[AgentDeploymentEvidenceConnectivity] = { 'offline', 'recent', 'unknown',  }

def check_agent_deployment_evidence_connectivity(value: str) -> AgentDeploymentEvidenceConnectivity:
    if value in AGENT_DEPLOYMENT_EVIDENCE_CONNECTIVITY_VALUES:
        return cast(AgentDeploymentEvidenceConnectivity, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {AGENT_DEPLOYMENT_EVIDENCE_CONNECTIVITY_VALUES!r}")

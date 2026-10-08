from typing import Literal

AgentDiagnosticOperation = Literal['controller.request', 'model.materialization_copy_fallback', 'workload.preload_memory']

AGENT_DIAGNOSTIC_OPERATION_VALUES: set[AgentDiagnosticOperation] = { 'controller.request', 'model.materialization_copy_fallback', 'workload.preload_memory',  }

def check_agent_diagnostic_operation(value: str) -> AgentDiagnosticOperation:
    if value in AGENT_DIAGNOSTIC_OPERATION_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {AGENT_DIAGNOSTIC_OPERATION_VALUES!r}")

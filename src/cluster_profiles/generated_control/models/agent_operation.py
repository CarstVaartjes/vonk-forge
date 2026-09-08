from typing import Literal, cast

AgentOperation = Literal['agent.upgrade.v1', 'artifact.distribution.v1', 'recipe.build.v1', 'recipe.image.import.v1', 'recipe.install', 'recipe.job.run.v1', 'recipe.model-uninstall.v1', 'recipe.start', 'recipe.stop', 'recipe.uninstall', 'runtime.preflight.v1']

AGENT_OPERATION_VALUES: set[AgentOperation] = { 'agent.upgrade.v1', 'artifact.distribution.v1', 'recipe.build.v1', 'recipe.image.import.v1', 'recipe.install', 'recipe.job.run.v1', 'recipe.model-uninstall.v1', 'recipe.start', 'recipe.stop', 'recipe.uninstall', 'runtime.preflight.v1',  }

def check_agent_operation(value: str) -> AgentOperation:
    if value in AGENT_OPERATION_VALUES:
        return cast(AgentOperation, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {AGENT_OPERATION_VALUES!r}")

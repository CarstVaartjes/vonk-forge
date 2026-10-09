"""Fleet reason codes."""

from ..wire_model import WireEnum


class ClusterMappingCode(WireEnum):
    """Refusals of a cluster mapping (recipe-to-Spark assignment) request."""

    ACTOR = "mapping.actor"
    ENDPOINT_OWNER = "mapping.endpoint_owner"
    NODE_COUNT = "mapping.node_count"
    NODE_INCOMPATIBLE = "mapping.node_incompatible"
    NODE_UNKNOWN = "mapping.node_unknown"
    NODES_INVALID = "mapping.nodes_invalid"
    OPTION_INVALID = "mapping.option_invalid"
    PARAMETER_TYPE = "mapping.parameter_type"
    PARAMETER_UNKNOWN = "mapping.parameter_unknown"
    PARAMETER_VALUE = "mapping.parameter_value"
    PARAMETERS_INVALID = "mapping.parameters_invalid"
    READY_IMMUTABLE = "mapping.ready_immutable"
    RECIPE_UNRESOLVED = "mapping.recipe_unresolved"
    STALE_PLAN = "mapping.stale_plan"
    TOPOLOGY_INVALID = "mapping.topology_invalid"


class DistributionCode(WireEnum):
    """Why a distribution assignment object cannot be served to a Spark."""

    ASSIGNMENT_CONFLICT = "distribution.assignment_conflict"
    EXPIRED = "distribution.expired"
    MODEL_SET_IDENTITY_UNAVAILABLE = "distribution.model_set_identity_unavailable"
    MODEL_SET_MISMATCH = "distribution.model_set_mismatch"
    OBJECT_INVALID = "distribution.object_invalid"
    OBJECT_UNAVAILABLE = "distribution.object_unavailable"
    RUNTIME_IMAGE_MISMATCH = "distribution.runtime_image_mismatch"
    UNASSIGNED = "distribution.unassigned"
    WRONG_NODE = "distribution.wrong_node"


class TopologyCode(WireEnum):
    """Topology planning refusals."""

    FABRIC_INSUFFICIENT = "topology.fabric_insufficient"
    INVALID = "topology.invalid"
    PLACEMENT_INVALID = "topology.placement_invalid"
    ROLE_MISMATCH = "topology.role_mismatch"
    RUNTIME_CAPABILITY_MISSING = "topology.runtime_capability_missing"

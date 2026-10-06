"""Which contract owns the document in every JSON column of ``models.py``.

``bind`` is the only place a JSON column gets its contract; a column with no
binding fails ``control/tests/test_json_column_contracts.py``.  A table that
stores several document families binds one contract per value of its
``kind`` column.  The mechanism (reading, writing, the write guard and the
structural walker) is :mod:`vonk_control.stored_json`.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from vonk_agent_protocol import CacheReferenceReason
from vonk_agent_protocol.inventory import Capability, NetworkInterface
from vonk_agent_protocol.route_activation import ActivationMarker

from .fleet_profile_contract import LabelName, LabelValue
from .recipe_execution_contract import StoredRunEndpoint
from .stored_documents import RouteClaimMarker
from .stored_json import bind

ProfileLabels = Annotated[dict[LabelName, LabelValue], Field(max_length=16)]

bind("jobs", "targets", list[str])
bind("agent_node_profiles", "labels", ProfileLabels)
bind("fleet_profiles", "labels", ProfileLabels)
bind("node_inventory_snapshots", "capabilities", list[Capability])
bind(
    "node_inventory_snapshots",
    "network_interfaces",
    list[NetworkInterface],
    nullable=True,
)
bind("model_cache_sets", "protected_reasons", list[CacheReferenceReason])
bind("run_nodes", "endpoint", StoredRunEndpoint, nullable=True)
bind(
    "route_publications",
    "activation_marker",
    ActivationMarker | RouteClaimMarker,
    nullable=True,
)

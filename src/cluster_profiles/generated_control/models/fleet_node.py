from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import cast, Union

if TYPE_CHECKING:
  from ..models.recipe_presence import RecipePresence
  from ..models.projection_reason import ProjectionReason
  from ..models.fleet_node_labels import FleetNodeLabels
  from ..models.capacity_reservations import CapacityReservations
  from ..models.inventory_state import InventoryState
  from ..models.run_presence import RunPresence
  from ..models.telemetry_state import TelemetryState
  from ..models.node_connection import NodeConnection





T = TypeVar("T", bound="FleetNode")



@_attrs_define
class FleetNode:
    """
        Attributes:
            connection (NodeConnection):
            display_name (str):
            hostname (str):
            id (str):
            installed (list['RecipePresence']):
            inventory (Union['InventoryState', None]):
            labels (FleetNodeLabels):
            lifecycle (str):
            loaded (list['RunPresence']):
            reservations (CapacityReservations):
            telemetry (Union['TelemetryState', None]):
            warnings (list['ProjectionReason']):
     """

    connection: 'NodeConnection'
    display_name: str
    hostname: str
    id: str
    installed: list['RecipePresence']
    inventory: Union['InventoryState', None]
    labels: 'FleetNodeLabels'
    lifecycle: str
    loaded: list['RunPresence']
    reservations: 'CapacityReservations'
    telemetry: Union['TelemetryState', None]
    warnings: list['ProjectionReason']





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_presence import RecipePresence
        from ..models.projection_reason import ProjectionReason
        from ..models.fleet_node_labels import FleetNodeLabels
        from ..models.capacity_reservations import CapacityReservations
        from ..models.inventory_state import InventoryState
        from ..models.run_presence import RunPresence
        from ..models.telemetry_state import TelemetryState
        from ..models.node_connection import NodeConnection
        connection = self.connection.to_dict()

        display_name = self.display_name

        hostname = self.hostname

        id = self.id

        installed = []
        for installed_item_data in self.installed:
            installed_item = installed_item_data.to_dict()
            installed.append(installed_item)



        inventory: Union[None, dict[str, Any]]
        if isinstance(self.inventory, InventoryState):
            inventory = self.inventory.to_dict()
        else:
            inventory = self.inventory

        labels = self.labels.to_dict()

        lifecycle = self.lifecycle

        loaded = []
        for loaded_item_data in self.loaded:
            loaded_item = loaded_item_data.to_dict()
            loaded.append(loaded_item)



        reservations = self.reservations.to_dict()

        telemetry: Union[None, dict[str, Any]]
        if isinstance(self.telemetry, TelemetryState):
            telemetry = self.telemetry.to_dict()
        else:
            telemetry = self.telemetry

        warnings = []
        for warnings_item_data in self.warnings:
            warnings_item = warnings_item_data.to_dict()
            warnings.append(warnings_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "connection": connection,
            "display_name": display_name,
            "hostname": hostname,
            "id": id,
            "installed": installed,
            "inventory": inventory,
            "labels": labels,
            "lifecycle": lifecycle,
            "loaded": loaded,
            "reservations": reservations,
            "telemetry": telemetry,
            "warnings": warnings,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_presence import RecipePresence
        from ..models.projection_reason import ProjectionReason
        from ..models.fleet_node_labels import FleetNodeLabels
        from ..models.capacity_reservations import CapacityReservations
        from ..models.inventory_state import InventoryState
        from ..models.run_presence import RunPresence
        from ..models.telemetry_state import TelemetryState
        from ..models.node_connection import NodeConnection
        d = dict(src_dict)
        connection = NodeConnection.from_dict(d.pop("connection"))




        display_name = d.pop("display_name")

        hostname = d.pop("hostname")

        id = d.pop("id")

        installed = []
        _installed = d.pop("installed")
        for installed_item_data in (_installed):
            installed_item = RecipePresence.from_dict(installed_item_data)



            installed.append(installed_item)


        def _parse_inventory(data: object) -> Union['InventoryState', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                inventory_type_0 = InventoryState.from_dict(data)



                return inventory_type_0
            except: # noqa: E722
                pass
            return cast(Union['InventoryState', None], data)

        inventory = _parse_inventory(d.pop("inventory"))


        labels = FleetNodeLabels.from_dict(d.pop("labels"))




        lifecycle = d.pop("lifecycle")

        loaded = []
        _loaded = d.pop("loaded")
        for loaded_item_data in (_loaded):
            loaded_item = RunPresence.from_dict(loaded_item_data)



            loaded.append(loaded_item)


        reservations = CapacityReservations.from_dict(d.pop("reservations"))




        def _parse_telemetry(data: object) -> Union['TelemetryState', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                telemetry_type_0 = TelemetryState.from_dict(data)



                return telemetry_type_0
            except: # noqa: E722
                pass
            return cast(Union['TelemetryState', None], data)

        telemetry = _parse_telemetry(d.pop("telemetry"))


        warnings = []
        _warnings = d.pop("warnings")
        for warnings_item_data in (_warnings):
            warnings_item = ProjectionReason.from_dict(warnings_item_data)



            warnings.append(warnings_item)


        fleet_node = cls(
            connection=connection,
            display_name=display_name,
            hostname=hostname,
            id=id,
            installed=installed,
            inventory=inventory,
            labels=labels,
            lifecycle=lifecycle,
            loaded=loaded,
            reservations=reservations,
            telemetry=telemetry,
            warnings=warnings,
        )

        return fleet_node

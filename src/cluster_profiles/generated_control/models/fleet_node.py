from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.capacity_reservations import CapacityReservations
  from ..models.fleet_node_labels_type_0 import FleetNodeLabelsType0
  from ..models.inventory_state import InventoryState
  from ..models.node_connection import NodeConnection
  from ..models.projection_reason import ProjectionReason
  from ..models.recipe_presence import RecipePresence
  from ..models.run_presence import RunPresence
  from ..models.telemetry_state import TelemetryState
  from ..models.unavailable_recipe_presence import UnavailableRecipePresence
  from ..models.unavailable_run_presence import UnavailableRunPresence





T = TypeVar("T", bound="FleetNode")



@_attrs_define
class FleetNode:
    """
        Attributes:
            connection (NodeConnection):
            display_name (str):
            hostname (str):
            id (str):
            installed (list[RecipePresence | UnavailableRecipePresence]):
            inventory (InventoryState | None):
            labels (FleetNodeLabelsType0 | None):
            lifecycle (str):
            loaded (list[RunPresence | UnavailableRunPresence]):
            reservations (CapacityReservations):
            telemetry (None | TelemetryState):
            warnings (list[ProjectionReason]):
            ip_address (None | str | Unset):
            projection_issues (list[str] | None | Unset):
     """

    connection: NodeConnection
    display_name: str
    hostname: str
    id: str
    installed: list[RecipePresence | UnavailableRecipePresence]
    inventory: InventoryState | None
    labels: FleetNodeLabelsType0 | None
    lifecycle: str
    loaded: list[RunPresence | UnavailableRunPresence]
    reservations: CapacityReservations
    telemetry: None | TelemetryState
    warnings: list[ProjectionReason]
    ip_address: None | str | Unset = UNSET
    projection_issues: list[str] | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.capacity_reservations import CapacityReservations # noqa: PLC0415
        from ..models.fleet_node_labels_type_0 import FleetNodeLabelsType0 # noqa: PLC0415
        from ..models.inventory_state import InventoryState # noqa: PLC0415
        from ..models.node_connection import NodeConnection # noqa: PLC0415
        from ..models.projection_reason import ProjectionReason # noqa: PLC0415
        from ..models.recipe_presence import RecipePresence # noqa: PLC0415
        from ..models.run_presence import RunPresence # noqa: PLC0415
        from ..models.telemetry_state import TelemetryState # noqa: PLC0415
        from ..models.unavailable_recipe_presence import UnavailableRecipePresence # noqa: PLC0415
        from ..models.unavailable_run_presence import UnavailableRunPresence # noqa: PLC0415
        connection = self.connection.to_dict()

        display_name = self.display_name

        hostname = self.hostname

        id = self.id

        installed = []
        for installed_item_data in self.installed:
            installed_item: dict[str, Any]
            if isinstance(installed_item_data, RecipePresence):
                installed_item = installed_item_data.to_dict()
            else:
                installed_item = installed_item_data.to_dict()

            installed.append(installed_item)



        inventory: dict[str, Any] | None
        if isinstance(self.inventory, InventoryState):
            inventory = self.inventory.to_dict()
        else:
            inventory = self.inventory

        labels: dict[str, Any] | None
        if isinstance(self.labels, FleetNodeLabelsType0):
            labels = self.labels.to_dict()
        else:
            labels = self.labels

        lifecycle = self.lifecycle

        loaded = []
        for loaded_item_data in self.loaded:
            loaded_item: dict[str, Any]
            if isinstance(loaded_item_data, RunPresence):
                loaded_item = loaded_item_data.to_dict()
            else:
                loaded_item = loaded_item_data.to_dict()

            loaded.append(loaded_item)



        reservations = self.reservations.to_dict()

        telemetry: dict[str, Any] | None
        if isinstance(self.telemetry, TelemetryState):
            telemetry = self.telemetry.to_dict()
        else:
            telemetry = self.telemetry

        warnings = []
        for warnings_item_data in self.warnings:
            warnings_item = warnings_item_data.to_dict()
            warnings.append(warnings_item)



        ip_address: None | str | Unset
        if isinstance(self.ip_address, Unset):
            ip_address = UNSET
        else:
            ip_address = self.ip_address

        projection_issues: list[str] | None | Unset
        if isinstance(self.projection_issues, Unset):
            projection_issues = UNSET
        elif isinstance(self.projection_issues, list):
            projection_issues = self.projection_issues


        else:
            projection_issues = self.projection_issues


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
        if ip_address is not UNSET:
            field_dict["ip_address"] = ip_address
        if projection_issues is not UNSET:
            field_dict["projection_issues"] = projection_issues

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.capacity_reservations import CapacityReservations # noqa: PLC0415
        from ..models.fleet_node_labels_type_0 import FleetNodeLabelsType0 # noqa: PLC0415
        from ..models.inventory_state import InventoryState # noqa: PLC0415
        from ..models.node_connection import NodeConnection # noqa: PLC0415
        from ..models.projection_reason import ProjectionReason # noqa: PLC0415
        from ..models.recipe_presence import RecipePresence # noqa: PLC0415
        from ..models.run_presence import RunPresence # noqa: PLC0415
        from ..models.telemetry_state import TelemetryState # noqa: PLC0415
        from ..models.unavailable_recipe_presence import UnavailableRecipePresence # noqa: PLC0415
        from ..models.unavailable_run_presence import UnavailableRunPresence # noqa: PLC0415
        d = dict(src_dict)
        connection = NodeConnection.from_dict(d.pop("connection"))




        display_name = d.pop("display_name")

        hostname = d.pop("hostname")

        id = d.pop("id")

        installed = []
        _installed = d.pop("installed")
        for installed_item_data in (_installed):
            def _parse_installed_item(data: object) -> RecipePresence | UnavailableRecipePresence:
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    installed_item_type_0 = RecipePresence.from_dict(data)



                    return installed_item_type_0
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                if not isinstance(data, dict):
                    raise TypeError()
                installed_item_type_1 = UnavailableRecipePresence.from_dict(data)



                return installed_item_type_1

            installed_item = _parse_installed_item(installed_item_data)

            installed.append(installed_item)


        def _parse_inventory(data: object) -> InventoryState | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                inventory_type_0 = InventoryState.from_dict(data)



                return inventory_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(InventoryState | None, data)

        inventory = _parse_inventory(d.pop("inventory"))


        def _parse_labels(data: object) -> FleetNodeLabelsType0 | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                labels_type_0 = FleetNodeLabelsType0.from_dict(data)



                return labels_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetNodeLabelsType0 | None, data)

        labels = _parse_labels(d.pop("labels"))


        lifecycle = d.pop("lifecycle")

        loaded = []
        _loaded = d.pop("loaded")
        for loaded_item_data in (_loaded):
            def _parse_loaded_item(data: object) -> RunPresence | UnavailableRunPresence:
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    loaded_item_type_0 = RunPresence.from_dict(data)



                    return loaded_item_type_0
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                if not isinstance(data, dict):
                    raise TypeError()
                loaded_item_type_1 = UnavailableRunPresence.from_dict(data)



                return loaded_item_type_1

            loaded_item = _parse_loaded_item(loaded_item_data)

            loaded.append(loaded_item)


        reservations = CapacityReservations.from_dict(d.pop("reservations"))




        def _parse_telemetry(data: object) -> None | TelemetryState:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                telemetry_type_0 = TelemetryState.from_dict(data)



                return telemetry_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TelemetryState, data)

        telemetry = _parse_telemetry(d.pop("telemetry"))


        warnings = []
        _warnings = d.pop("warnings")
        for warnings_item_data in (_warnings):
            warnings_item = ProjectionReason.from_dict(warnings_item_data)



            warnings.append(warnings_item)


        def _parse_ip_address(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        ip_address = _parse_ip_address(d.pop("ip_address", UNSET))


        def _parse_projection_issues(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                projection_issues_type_0 = cast(list[str], data)

                return projection_issues_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        projection_issues = _parse_projection_issues(d.pop("projection_issues", UNSET))


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
            ip_address=ip_address,
            projection_issues=projection_issues,
        )

        return fleet_node

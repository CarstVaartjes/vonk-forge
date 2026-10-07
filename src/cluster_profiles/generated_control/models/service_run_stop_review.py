from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.route_state import check_route_state
from ..models.route_state import RouteState
from ..models.service_run_stop_review_stage import check_service_run_stop_review_stage
from ..models.service_run_stop_review_stage import ServiceRunStopReviewStage
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.profile_stop_owner_binding import ProfileStopOwnerBinding
  from ..models.service_run_stop_review_exact_payloads_type_0 import ServiceRunStopReviewExactPayloadsType0





T = TypeVar("T", bound="ServiceRunStopReview")



@_attrs_define
class ServiceRunStopReview:
    """ The exact service Stop accepted before its route withdrawal claim.

        Attributes:
            missing_node_ids (list[str]):
            route_state (RouteState): Whether a run's inference route is published to the gateway.
            run_generation (int):
            stage (ServiceRunStopReviewStage):
            target_node_ids (list[str]):
            exact_payloads (None | ServiceRunStopReviewExactPayloadsType0 | Unset):
            profile_application_id (None | str | Unset):
            profile_stop_owner (None | ProfileStopOwnerBinding | Unset):
            profile_target_node_ids (list[str] | None | Unset):
            stop_order (list[str] | None | Unset):
     """

    missing_node_ids: list[str]
    route_state: RouteState
    run_generation: int
    stage: ServiceRunStopReviewStage
    target_node_ids: list[str]
    exact_payloads: None | ServiceRunStopReviewExactPayloadsType0 | Unset = UNSET
    profile_application_id: None | str | Unset = UNSET
    profile_stop_owner: None | ProfileStopOwnerBinding | Unset = UNSET
    profile_target_node_ids: list[str] | None | Unset = UNSET
    stop_order: list[str] | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.profile_stop_owner_binding import ProfileStopOwnerBinding # noqa: PLC0415
        from ..models.service_run_stop_review_exact_payloads_type_0 import ServiceRunStopReviewExactPayloadsType0 # noqa: PLC0415
        missing_node_ids = self.missing_node_ids



        route_state: str = self.route_state

        run_generation = self.run_generation

        stage: str = self.stage

        target_node_ids = self.target_node_ids



        exact_payloads: dict[str, Any] | None | Unset
        if isinstance(self.exact_payloads, Unset):
            exact_payloads = UNSET
        elif isinstance(self.exact_payloads, ServiceRunStopReviewExactPayloadsType0):
            exact_payloads = self.exact_payloads.to_dict()
        else:
            exact_payloads = self.exact_payloads

        profile_application_id: None | str | Unset
        if isinstance(self.profile_application_id, Unset):
            profile_application_id = UNSET
        else:
            profile_application_id = self.profile_application_id

        profile_stop_owner: dict[str, Any] | None | Unset
        if isinstance(self.profile_stop_owner, Unset):
            profile_stop_owner = UNSET
        elif isinstance(self.profile_stop_owner, ProfileStopOwnerBinding):
            profile_stop_owner = self.profile_stop_owner.to_dict()
        else:
            profile_stop_owner = self.profile_stop_owner

        profile_target_node_ids: list[str] | None | Unset
        if isinstance(self.profile_target_node_ids, Unset):
            profile_target_node_ids = UNSET
        elif isinstance(self.profile_target_node_ids, list):
            profile_target_node_ids = self.profile_target_node_ids


        else:
            profile_target_node_ids = self.profile_target_node_ids

        stop_order: list[str] | None | Unset
        if isinstance(self.stop_order, Unset):
            stop_order = UNSET
        elif isinstance(self.stop_order, list):
            stop_order = self.stop_order


        else:
            stop_order = self.stop_order


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "missing_node_ids": missing_node_ids,
            "route_state": route_state,
            "run_generation": run_generation,
            "stage": stage,
            "target_node_ids": target_node_ids,
        })
        if exact_payloads is not UNSET:
            field_dict["exact_payloads"] = exact_payloads
        if profile_application_id is not UNSET:
            field_dict["profile_application_id"] = profile_application_id
        if profile_stop_owner is not UNSET:
            field_dict["profile_stop_owner"] = profile_stop_owner
        if profile_target_node_ids is not UNSET:
            field_dict["profile_target_node_ids"] = profile_target_node_ids
        if stop_order is not UNSET:
            field_dict["stop_order"] = stop_order

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.profile_stop_owner_binding import ProfileStopOwnerBinding # noqa: PLC0415
        from ..models.service_run_stop_review_exact_payloads_type_0 import ServiceRunStopReviewExactPayloadsType0 # noqa: PLC0415
        d = dict(src_dict)
        missing_node_ids = cast(list[str], d.pop("missing_node_ids"))


        route_state = check_route_state(d.pop("route_state"))




        run_generation = d.pop("run_generation")

        stage = check_service_run_stop_review_stage(d.pop("stage"))




        target_node_ids = cast(list[str], d.pop("target_node_ids"))


        def _parse_exact_payloads(data: object) -> None | ServiceRunStopReviewExactPayloadsType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                exact_payloads_type_0 = ServiceRunStopReviewExactPayloadsType0.from_dict(data)



                return exact_payloads_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ServiceRunStopReviewExactPayloadsType0 | Unset, data)

        exact_payloads = _parse_exact_payloads(d.pop("exact_payloads", UNSET))


        def _parse_profile_application_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_application_id = _parse_profile_application_id(d.pop("profile_application_id", UNSET))


        def _parse_profile_stop_owner(data: object) -> None | ProfileStopOwnerBinding | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                profile_stop_owner_type_0 = ProfileStopOwnerBinding.from_dict(data)



                return profile_stop_owner_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProfileStopOwnerBinding | Unset, data)

        profile_stop_owner = _parse_profile_stop_owner(d.pop("profile_stop_owner", UNSET))


        def _parse_profile_target_node_ids(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                profile_target_node_ids_type_0 = cast(list[str], data)

                return profile_target_node_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        profile_target_node_ids = _parse_profile_target_node_ids(d.pop("profile_target_node_ids", UNSET))


        def _parse_stop_order(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                stop_order_type_0 = cast(list[str], data)

                return stop_order_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        stop_order = _parse_stop_order(d.pop("stop_order", UNSET))


        service_run_stop_review = cls(
            missing_node_ids=missing_node_ids,
            route_state=route_state,
            run_generation=run_generation,
            stage=stage,
            target_node_ids=target_node_ids,
            exact_payloads=exact_payloads,
            profile_application_id=profile_application_id,
            profile_stop_owner=profile_stop_owner,
            profile_target_node_ids=profile_target_node_ids,
            stop_order=stop_order,
        )

        return service_run_stop_review

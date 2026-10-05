"""The NAS-route warning codes are one typed set shared by every client."""

from typing import get_args

from vonk_agent_protocol.inventory import NetworkInterface
from vonk_control.fleet_projection import ProjectionReason
from vonk_control.nas_route_notice import NasRouteWarningCode, nas_route_notice


def test_every_nas_route_code_is_a_projection_reason_code() -> None:
    # The guard for the class: a new notice code that the projection contract
    # does not name would fail validation at runtime, so fail here instead.
    reason_codes = set(get_args(ProjectionReason.model_fields["code"].annotation))
    assert set(get_args(NasRouteWarningCode)) <= reason_codes
    assert {code for code in reason_codes if code.startswith("network.")} == set(
        get_args(NasRouteWarningCode)
    )


def test_notice_names_the_route_and_the_wired_port_state() -> None:
    wifi = NetworkInterface(
        name="wlP9s9", kind="wifi", link_speed_mbps=None, carrier=True
    )
    port = NetworkInterface(name="enP7s7", kind="wired", carrier=False)
    notice = nas_route_notice([port, wifi], "wlP9s9")
    assert notice is not None
    assert notice.code == "network.nas-route-wifi-wired-port-down"
    assert "unknown speed" in notice.detail
    assert nas_route_notice([port, wifi], "enP7s7") is None
    assert nas_route_notice(None, "wlP9s9") is None

"""The NAS-route warning codes are one typed set shared by every client."""

from typing import get_args

from vonk_agent_protocol import ProjectionCode
from vonk_agent_protocol.inventory import NetworkInterface
from vonk_control.fleet_projection import ProjectionReason
from vonk_control.nas_route_notice import NasRouteWarningCode, nas_route_notice


def test_every_nas_route_code_is_a_projection_reason_code() -> None:
    # The guard for the class: a new notice code that the projection contract
    # does not name would fail validation at runtime, so fail here instead.
    assert ProjectionReason.model_fields["code"].annotation is ProjectionCode
    reason_codes = set(ProjectionCode)
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
    assert "unknown" not in notice.detail
    assert "Wi-Fi (wlP9s9, shared airtime)" in notice.detail
    assert nas_route_notice([port, wifi], "enP7s7") is None
    assert nas_route_notice(None, "wlP9s9") is None


def test_fabric_ports_are_never_the_recommended_nas_port() -> None:
    wifi = NetworkInterface(name="wlP9s9", kind="wifi", carrier=True)
    fabric = NetworkInterface(
        name="enP2p1s0f1np1", kind="fabric", link_speed_mbps=200_000, carrier=True
    )
    rj45 = NetworkInterface(name="enP7s7", kind="wired", carrier=False)
    notice = nas_route_notice([fabric, rj45, wifi], "wlP9s9")
    assert notice is not None
    assert notice.code == "network.nas-route-wifi-wired-port-down"
    assert "RJ45 port enP7s7" in notice.recommendation
    assert "enP2p1s0f1np1" not in notice.recommendation
    notice = nas_route_notice([fabric, wifi], "wlP9s9")
    assert notice is not None
    assert notice.code == "network.nas-route-wifi-no-wired-port"


def test_a_route_over_a_tunnel_is_reported_as_such_and_never_warns() -> None:
    """Catches treating a Tailscale/IPv6 NAS route as a Wi-Fi or missing-port finding."""

    tunnel = NetworkInterface(name="tailscale0", kind="tunnel", carrier=True)
    wifi = NetworkInterface(name="wlP9s9", kind="wifi", carrier=True)
    down = NetworkInterface(name="enP7s7", kind="wired", carrier=False)
    # Wi-Fi is up and the wired port is down, but the NAS is reached over the
    # overlay: the one route that matters is not Wi-Fi.
    assert nas_route_notice([down, wifi, tunnel], "tailscale0") is None
    assert tunnel.kind == "tunnel"

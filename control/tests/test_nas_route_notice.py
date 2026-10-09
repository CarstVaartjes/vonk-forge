"""The NAS-route warning codes are one typed set shared by every client."""

from vonk_agent_protocol.inventory import NetworkInterface
from vonk_control.fleet_projection import ProjectionReason
from vonk_control.nas_route_notice import nas_route_notice


def test_notice_roundtrips_through_the_projection_consumer() -> None:
    wifi = NetworkInterface(name="wlan0", kind="wifi", carrier=True)
    for ports in (
        [],
        [NetworkInterface(name="eth0", kind="wired", carrier=False)],
        [NetworkInterface(name="eth0", kind="wired", carrier=True)],
    ):
        notice = nas_route_notice([*ports, wifi], wifi.name)
        assert notice is not None
        reason = ProjectionReason(
            code=notice.code,
            detail=notice.detail,
            severity="warning",
            recommendation=notice.recommendation,
        )
        consumed = ProjectionReason.model_validate_json(reason.model_dump_json())
        assert consumed == reason
        assert nas_route_notice([*ports, wifi], "eth0") is None


def test_notice_names_the_route_and_the_wired_port_state() -> None:
    wifi = NetworkInterface(
        name="wlP9s9", kind="wifi", link_speed_mbps=None, carrier=True
    )
    port = NetworkInterface(name="enP7s7", kind="wired", carrier=False)
    notice = nas_route_notice([port, wifi], "wlP9s9")
    assert notice is not None
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
    assert "RJ45 port enP7s7" in notice.recommendation
    assert "enP2p1s0f1np1" not in notice.recommendation
    notice = nas_route_notice([fabric, wifi], "wlP9s9")
    assert notice is not None


def test_a_route_over_a_tunnel_is_reported_as_such_and_never_warns() -> None:
    """Catches treating a Tailscale/IPv6 NAS route as a Wi-Fi or missing-port finding."""

    tunnel = NetworkInterface(name="tailscale0", kind="tunnel", carrier=True)
    wifi = NetworkInterface(name="wlP9s9", kind="wifi", carrier=True)
    down = NetworkInterface(name="enP7s7", kind="wired", carrier=False)
    # Wi-Fi is up and the wired port is down, but the NAS is reached over the
    # overlay: the one route that matters is not Wi-Fi.
    assert nas_route_notice([down, wifi, tunnel], "tailscale0") is None
    assert tunnel.kind == "tunnel"

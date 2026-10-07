from typing import Literal

ProjectionCode = Literal['cpu.low-clock', 'fleet.frame_budget_exceeded', 'fleet.frame_encoding_unavailable', 'fleet.stored_event_payload_unavailable', 'install.partial', 'inventory.missing', 'inventory.stale', 'network.nas-route-wifi-no-wired-port', 'network.nas-route-wifi-wired-port-down', 'network.nas-route-wifi-wired-port-unused', 'node.offline', 'observation.transfer_unavailable', 'profile.retrying', 'recipe.update_available', 'run.degraded', 'telemetry.delayed', 'telemetry.missing', 'telemetry.stale']

PROJECTION_CODE_VALUES: set[ProjectionCode] = { 'cpu.low-clock', 'fleet.frame_budget_exceeded', 'fleet.frame_encoding_unavailable', 'fleet.stored_event_payload_unavailable', 'install.partial', 'inventory.missing', 'inventory.stale', 'network.nas-route-wifi-no-wired-port', 'network.nas-route-wifi-wired-port-down', 'network.nas-route-wifi-wired-port-unused', 'node.offline', 'observation.transfer_unavailable', 'profile.retrying', 'recipe.update_available', 'run.degraded', 'telemetry.delayed', 'telemetry.missing', 'telemetry.stale',  }

def check_projection_code(value: str) -> ProjectionCode:
    if value in PROJECTION_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROJECTION_CODE_VALUES!r}")

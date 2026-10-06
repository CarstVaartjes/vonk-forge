from typing import Literal

NetworkInterfaceKind = Literal['fabric', 'other', 'tunnel', 'wifi', 'wired']

NETWORK_INTERFACE_KIND_VALUES: set[NetworkInterfaceKind] = { 'fabric', 'other', 'tunnel', 'wifi', 'wired',  }

def check_network_interface_kind(value: str) -> NetworkInterfaceKind:
    if value in NETWORK_INTERFACE_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {NETWORK_INTERFACE_KIND_VALUES!r}")

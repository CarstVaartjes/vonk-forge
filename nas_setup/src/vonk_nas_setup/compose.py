"""Resolve the selected Compose profiles into the Compose file the NAS runs.

The NAS Docker UI runs `docker compose up` without profile flags, so a service
that carries `profiles:` would never be pulled or recreated there. The written
file therefore holds exactly the selected services and no `profiles:` key; the
selection (`COMPOSE_PROFILES` in `.env`) is resolved here once.
"""

from __future__ import annotations

import yaml

from .environment import SELECTION_ENV, profiles
from .filesystem import SetupError

CONSUMER = "VONK_SELECTED_PROFILES"
_SECTIONS = ("networks", "secrets", "volumes")


def _used(service: dict, section: str) -> set[str]:
    entries = service.get(section) or []
    names: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            names.add(entry.split(":", 1)[0] if section == "volumes" else entry)
        elif isinstance(entry, dict) and isinstance(entry.get("source"), str):
            names.add(entry["source"])
    return names


def resolve_compose(compose: str, environment: dict[str, str]) -> bytes:
    """Return the Compose file for the profiles selected in `environment`."""
    selected = profiles(environment.get(SELECTION_ENV, ""))
    document = yaml.safe_load(compose)
    services = document.get("services") or {}
    kept = {}
    for name, service in services.items():
        members = service.pop("profiles", None)
        if not members or set(members) & set(selected):
            kept[name] = service
    for name, service in kept.items():
        dependencies = service.get("depends_on") or []
        missing = {
            dependency
            for dependency in dependencies
            if dependency not in kept
            and (
                not isinstance(dependencies, dict)
                or dependencies[dependency].get("required") is not False
            )
        }
        if missing:
            raise SetupError(f"service {name} depends on unselected {sorted(missing)}")
        if str(service.get("network_mode", "")).removeprefix("service:") in (
            set(services) - set(kept)
        ):
            raise SetupError(f"service {name} shares the network of an unselected one")
    document["services"] = kept
    for section in _SECTIONS:
        if section not in document:
            continue
        used_before = set().union(*(_used(s, section) for s in services.values()))
        used_now = set().union(*(_used(s, section) for s in kept.values()))
        document[section] = {
            name: value
            for name, value in document[section].items()
            if name not in used_before or name in used_now
        }
    # Compose does not pass its own selection into containers; the consumer
    # gets the same literal that .env holds. A renamed variable fails here.
    found = False
    for service in services.values():
        container = service.get("environment")
        if isinstance(container, dict) and CONSUMER in container:
            if container[CONSUMER] != f"${{{SELECTION_ENV}:-}}":
                raise SetupError(f"{CONSUMER} must be ${{{SELECTION_ENV}:-}}")
            container[CONSUMER] = ",".join(selected)
            found = True
    if CONSUMER in compose and not found:
        raise SetupError(f"{CONSUMER} is not a mapping entry")
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False).encode()

"""Recover unambiguous site configuration and render Compose dotenv values."""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path

from vonk_agent_protocol.installer_setup import (
    NasInstallTemplate,
    NasRequiredValuePrompt,
    valid_required_value,
    validate_environment_name,
)

from .filesystem import read_member


def parse_environment(path: Path) -> dict[str, str]:
    document = read_member(path.parent, path.name)
    if document is None:
        return {}
    try:
        lines = document.decode().splitlines()
    except UnicodeDecodeError:
        return {}
    values: dict[str, str] = {}
    ambiguous: set[str] = set()
    for line in lines:
        name, separator, raw_value = line.partition("=")
        if not separator or "\0" in raw_value:
            continue
        try:
            validate_environment_name(name)
            value = json.loads(raw_value) if raw_value.startswith('"') else raw_value
            if not isinstance(value, str) or "\0" in value:
                continue
        except ValueError:
            continue
        if name in values and values[name] != value:
            ambiguous.add(name)
        else:
            values[name] = value
    return {name: value for name, value in values.items() if name not in ambiguous}


def render_environment(values: dict[str, str]) -> bytes:
    lines = []
    for name, value in values.items():
        validate_environment_name(name)
        if "\0" in value:
            raise ValueError(f"value for {name} contains NUL")
        encoded = (
            value
            if re.fullmatch(r"[A-Za-z0-9._:/-]*", value)
            else json.dumps(value, ensure_ascii=False)
        )
        lines.append(f"{name}={encoded}\n")
    return "".join(lines).encode()


def required_default(
    prompt: NasRequiredValuePrompt, environment: dict[str, str]
) -> str | None:
    if prompt.default is not None:
        return prompt.default
    if prompt.env == "VONK_MANAGEMENT_CIDRS":
        try:
            address = ipaddress.IPv4Address(environment["NAS_LAN_IP"])
        except (KeyError, ValueError):
            return None
        return str(ipaddress.IPv4Network(f"{address}/24", strict=False))
    return None


def profiles(value: str) -> list[str]:
    return [member.strip() for member in value.split(",") if member.strip()]


def configure_hermes(
    payload: NasInstallTemplate,
    environment: dict[str, str],
    requested: bool | None,
) -> bool | None:
    hermes = payload.hermes
    if hermes is None:
        return None
    members = profiles(environment.get(hermes.env, ""))
    enabled = hermes.enabled_value in members if requested is None else requested
    members = [
        member
        for member in members
        if member not in (hermes.enabled_value, hermes.disabled_value)
    ]
    if enabled:
        members.append(hermes.enabled_value)
    environment[hermes.env] = ",".join(members) if members else hermes.disabled_value
    return enabled


def retain_known(
    payload: NasInstallTemplate, environment: dict[str, str]
) -> tuple[str, ...]:
    known = {item.env for item in payload.internal_values}
    known.update(item.env for item in payload.required_values)
    known.update(payload.optional_values)
    if payload.hermes:
        known.add(payload.hermes.env)
    if payload.install_modes:
        known.add("COMPOSE_PROFILES")
        known.update(item.env for item in payload.install_modes.lab_values)
    dropped = tuple(name for name in environment if name not in known)
    for name in dropped:
        del environment[name]
    return dropped


def recover_environment(payload: NasInstallTemplate, bundle: Path) -> dict[str, str]:
    environment = parse_environment(bundle / ".env")
    retained = parse_environment(bundle / ".vonk-verified-secrets" / ".env")
    for name, value in retained.items():
        environment.setdefault(name, value)
    for required in payload.required_values:
        if required.env in environment and not valid_required_value(
            environment[required.env], required.validation
        ):
            environment.pop(required.env)
            if required.env in retained and valid_required_value(
                retained[required.env], required.validation
            ):
                environment[required.env] = retained[required.env]
    for item in payload.internal_values:
        environment[item.env] = item.value
    return environment

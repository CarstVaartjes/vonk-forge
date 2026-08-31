"""Closed declarative protocol for digest-bound recipe lifecycle work."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any

from .contracts import (
    AgentOperation,
    AgentProtocolError,
    _fields,
    _mapping,
    _uuid,
    _version,
)

RECIPE_OPERATIONS = frozenset(
    {
        AgentOperation.RECIPE_INSTALL,
        AgentOperation.RECIPE_START,
        AgentOperation.RECIPE_STOP,
        AgentOperation.RECIPE_UNINSTALL,
        AgentOperation.RECIPE_MODEL_UNINSTALL,
    }
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ALIAS = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?\Z")
_ROLE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise AgentProtocolError(f"{name} must be a lowercase SHA-256")
    return value


def _bytes(value: object, name: str, *, positive: bool = False) -> int:
    floor = 1 if positive else 0
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not floor <= value <= 16 * 1024**4
    ):
        raise AgentProtocolError(f"{name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class RecipeOperationRequest:
    operation: AgentOperation
    schema_version: int
    plan_digest: str
    installation_id: str | None = None
    recipe_revision_id: str | None = None
    recipe_content_sha256: str | None = None
    mapping_id: str | None = None
    mapping_generation: int | None = None
    recipe_build_id: str | None = None
    image_digest: str | None = None
    expected_bytes: int | None = None
    run_id: str | None = None
    alias: str | None = None
    rank: int | None = None
    role: str | None = None
    port: int | None = None
    reserved_memory_bytes: int | None = None
    endpoint_address: str | None = None
    world_size: int | None = None
    local_address: str | None = None
    master_address: str | None = None
    master_port: int | None = None
    cleanup_model_version_sha256: str | None = None
    model_version_sha256: str | None = None
    installations: tuple[tuple[str, str], ...] = ()

    @classmethod
    def parse(cls, operation: AgentOperation, payload: Any) -> RecipeOperationRequest:
        if operation not in RECIPE_OPERATIONS:
            raise AgentProtocolError("recipe operation is not supported")
        value = _mapping(payload)
        common = {"schema_version", "plan_digest"}
        if operation is AgentOperation.RECIPE_INSTALL:
            required = common | {
                "installation_id",
                "recipe_revision_id",
                "recipe_content_sha256",
                "mapping_id",
                "mapping_generation",
                "recipe_build_id",
                "image_digest",
                "rank",
                "role",
                "expected_bytes",
            }
        elif operation is AgentOperation.RECIPE_START:
            required = common | {
                "run_id",
                "installation_id",
                "recipe_revision_id",
                "recipe_content_sha256",
                "mapping_id",
                "mapping_generation",
                "image_digest",
                "alias",
                "rank",
                "role",
                "port",
                "reserved_memory_bytes",
                "endpoint_address",
                "world_size",
                "local_address",
                "master_address",
                "master_port",
            }
        elif operation is AgentOperation.RECIPE_STOP:
            required = common | {"run_id"}
        elif operation is AgentOperation.RECIPE_MODEL_UNINSTALL:
            required = common | {"model_version_sha256", "installations"}
        else:
            required = common | {"installation_id", "recipe_content_sha256"}
            if "cleanup_model_version_sha256" in value:
                required.add("cleanup_model_version_sha256")
        _fields(value, required=required)
        schema_version = _version(value["schema_version"])
        plan_digest = _digest(value["plan_digest"], "plan_digest")
        installation_id = (
            _uuid(value["installation_id"], name="installation_id")
            if "installation_id" in value
            else None
        )
        recipe_revision_id = (
            _uuid(value["recipe_revision_id"], name="recipe_revision_id")
            if "recipe_revision_id" in value
            else None
        )
        recipe_digest = (
            _digest(value["recipe_content_sha256"], "recipe_content_sha256")
            if "recipe_content_sha256" in value
            else None
        )
        cleanup_model_digest = (
            _digest(
                value["cleanup_model_version_sha256"],
                "cleanup_model_version_sha256",
            )
            if value.get("cleanup_model_version_sha256") is not None
            else None
        )
        model_digest = (
            _digest(value["model_version_sha256"], "model_version_sha256")
            if "model_version_sha256" in value
            else None
        )
        installations: tuple[tuple[str, str], ...] = ()
        if operation is AgentOperation.RECIPE_MODEL_UNINSTALL:
            raw_installations = value["installations"]
            if (
                not isinstance(raw_installations, list)
                or not 1 <= len(raw_installations) <= 512
            ):
                raise AgentProtocolError("model uninstall installations are invalid")
            parsed_installations: list[tuple[str, str]] = []
            for raw_installation in raw_installations:
                item = _mapping(raw_installation)
                _fields(
                    item,
                    required={"installation_id", "recipe_content_sha256"},
                )
                parsed_installations.append(
                    (
                        _uuid(item["installation_id"], name="installation_id"),
                        _digest(
                            item["recipe_content_sha256"],
                            "recipe_content_sha256",
                        ),
                    )
                )
            installations = tuple(parsed_installations)
            if len({item[0] for item in installations}) != len(installations):
                raise AgentProtocolError("model uninstall installations are duplicated")
        expected_bytes = (
            _bytes(value["expected_bytes"], "expected_bytes")
            if "expected_bytes" in value
            else None
        )
        mapping_id = (
            _uuid(value["mapping_id"], name="mapping_id")
            if "mapping_id" in value
            else None
        )
        mapping_generation = value.get("mapping_generation")
        if "mapping_generation" in value and (
            not isinstance(mapping_generation, int)
            or isinstance(mapping_generation, bool)
            or mapping_generation < 1
        ):
            raise AgentProtocolError("mapping generation is invalid")
        recipe_build_id = (
            _uuid(value["recipe_build_id"], name="recipe_build_id")
            if "recipe_build_id" in value
            else None
        )
        image_digest = value.get("image_digest")
        if "image_digest" in value and (
            not isinstance(image_digest, str)
            or _OCI_DIGEST.fullmatch(image_digest) is None
        ):
            raise AgentProtocolError("image digest is invalid")
        run_id = _uuid(value["run_id"], name="run_id") if "run_id" in value else None
        alias = value.get("alias")
        rank = value.get("rank")
        role = value.get("role")
        port = value.get("port")
        reserved_memory = value.get("reserved_memory_bytes")
        endpoint_address = value.get("endpoint_address")
        world_size = value.get("world_size")
        local_address = value.get("local_address")
        master_address = value.get("master_address")
        master_port = value.get("master_port")
        if operation is AgentOperation.RECIPE_INSTALL and (
            not isinstance(rank, int)
            or isinstance(rank, bool)
            or rank < 0
            or not isinstance(role, str)
            or _ROLE.fullmatch(role) is None
        ):
            raise AgentProtocolError("recipe install placement is invalid")
        if operation is AgentOperation.RECIPE_START:
            if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
                raise AgentProtocolError("recipe alias is invalid")
            if (
                not isinstance(rank, int)
                or isinstance(rank, bool)
                or rank < 0
                or not isinstance(role, str)
                or _ROLE.fullmatch(role) is None
                or not isinstance(port, int)
                or isinstance(port, bool)
                or not 1024 <= port <= 65535
            ):
                raise AgentProtocolError("recipe start placement is invalid")
            if (
                not isinstance(world_size, int)
                or isinstance(world_size, bool)
                or not 1 <= world_size <= 2**32 - 1
                or rank >= world_size
            ):
                raise AgentProtocolError("recipe start world size is invalid")
            try:
                address = ipaddress.ip_address(endpoint_address)
            except (TypeError, ValueError) as error:
                raise AgentProtocolError(
                    "recipe endpoint address is invalid"
                ) from error
            if (
                address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
                or str(address) != endpoint_address
            ):
                raise AgentProtocolError("recipe endpoint address is invalid")
            if world_size == 1:
                if (
                    rank != 0
                    or local_address is not None
                    or master_address is not None
                    or master_port is not None
                ):
                    raise AgentProtocolError("recipe single-node rendezvous is invalid")
            else:
                for candidate in (local_address, master_address):
                    try:
                        fabric = ipaddress.ip_address(candidate)
                    except (TypeError, ValueError) as error:
                        raise AgentProtocolError(
                            "recipe fabric address is invalid"
                        ) from error
                    if (
                        fabric.is_loopback
                        or fabric.is_link_local
                        or fabric.is_multicast
                        or fabric.is_unspecified
                        or str(fabric) != candidate
                    ):
                        raise AgentProtocolError("recipe fabric address is invalid")
                if (
                    not isinstance(master_port, int)
                    or isinstance(master_port, bool)
                    or not 1024 <= master_port <= 65535
                ):
                    raise AgentProtocolError("recipe fabric port is invalid")
            reserved_memory = _bytes(
                reserved_memory, "reserved_memory_bytes", positive=True
            )
        return cls(
            operation=operation,
            schema_version=schema_version,
            plan_digest=plan_digest,
            installation_id=installation_id,
            recipe_revision_id=recipe_revision_id,
            recipe_content_sha256=recipe_digest,
            mapping_id=mapping_id,
            mapping_generation=(
                mapping_generation if isinstance(mapping_generation, int) else None
            ),
            recipe_build_id=recipe_build_id,
            image_digest=image_digest if isinstance(image_digest, str) else None,
            expected_bytes=expected_bytes,
            run_id=run_id,
            alias=alias if isinstance(alias, str) else None,
            rank=rank if isinstance(rank, int) and not isinstance(rank, bool) else None,
            role=role if isinstance(role, str) else None,
            port=port if isinstance(port, int) and not isinstance(port, bool) else None,
            reserved_memory_bytes=reserved_memory
            if isinstance(reserved_memory, int)
            else None,
            endpoint_address=endpoint_address
            if isinstance(endpoint_address, str)
            else None,
            world_size=world_size if isinstance(world_size, int) else None,
            local_address=local_address if isinstance(local_address, str) else None,
            master_address=master_address if isinstance(master_address, str) else None,
            master_port=master_port if isinstance(master_port, int) else None,
            cleanup_model_version_sha256=cleanup_model_digest,
            model_version_sha256=model_digest,
            installations=installations,
        )


__all__ = ["RECIPE_OPERATIONS", "RecipeOperationRequest"]

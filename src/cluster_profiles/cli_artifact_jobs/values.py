"""Patterns and small checks shared by every artifact job module."""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable, Mapping

from cluster_profiles.control_client.errors import ControlMalformedResponse

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ControlMalformedResponse(f"{label} are invalid")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ControlMalformedResponse(f"{label} is invalid")
    return value


def _json_mode(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "global_json", False) or getattr(args, "json", False))


def _stable_request_key(
    args: argparse.Namespace,
    factory: Callable[[], str],
    resolver: Callable[[argparse.Namespace, Callable[[], str]], str],
) -> str:
    value = resolver(args, factory)
    if _UUID.fullmatch(value) is None:
        raise ValueError("request key must be a version 1–5 UUID")
    return value

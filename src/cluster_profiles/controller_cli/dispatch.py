"""Controller namespace dispatch and gateway key commands."""

from __future__ import annotations

import argparse
from collections.abc import Callable

from ..cli_files import PrivateOutput
from .common import ControllerClient, _quoted
from .confirmation import _confirm_action
from .fleet import _fleet
from .library import _model, _recipe
from .profile import _profile
from .run import _run


def run_controller(
    args: argparse.Namespace,
    client: ControllerClient,
    request_id_factory: Callable[[], str],
) -> dict[str, object]:
    command = getattr(args, "command", None) or "profile"
    if command == "platform":
        return client.request("GET", "/api/platform")
    if command == "run":
        return _run(args, client, request_id_factory)
    if command == "fleet":
        return _fleet(args, client, request_id_factory)
    if command == "model":
        return _model(args, client, request_id_factory)
    if command == "recipe":
        return _recipe(args, client, request_id_factory)
    if command == "profile":
        return _profile(args, client, request_id_factory)
    if command == "key":
        return _key(args, client)
    raise ValueError(f"unsupported controller command: {command}")


def _key(args: argparse.Namespace, client: ControllerClient) -> dict[str, object]:
    action = getattr(args, "key_action", None)
    if action in (None, "list"):
        return client.request("GET", "/api/key")
    if action == "revoke":
        _confirm_action(
            args, f"Revoke key {args.name}? Apps using it stop working immediately."
        )
        return client.request("POST", f"/api/key/{_quoted(args.name)}/revoke")
    if action == "roll":
        _confirm_action(
            args,
            f"Roll key {args.name}? Apps using the old secret stop working immediately.",
        )
        # Revoke and roll take no request body; sending ``{}`` fails the
        # client's own OpenAPI request check before anything is sent.
        path, payload = f"/api/key/{_quoted(args.name)}/roll", None
    else:
        path = "/api/key"
        payload = {"name": args.name, "models": args.models}
        if args.expires is not None:
            payload["expires"] = args.expires
    if args.output is None:
        return client.request("POST", path, payload)
    # Reserve the private file before the key exists so it cannot be lost.
    with PrivateOutput(args.output) as destination:
        result = client.request("POST", path, payload)
        destination.write_bytes(f"{result.pop('key')}\n".encode())
    result["output"] = str(args.output.absolute())
    return result

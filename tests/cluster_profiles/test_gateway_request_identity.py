"""A lost key response keeps the CLI's exact request identity on replay."""

from __future__ import annotations

import httpx2
import pytest

from cluster_profiles import cli
from cluster_profiles.control_client import _validate_generated_request
from cluster_profiles.controller_cli.dispatch import run_controller
from cluster_profiles.generated_control.models.gateway_key_created import (
    GatewayKeyCreated,
)
from tests.cluster_profiles.test_controller_cli import FakeClient

FIRST = "10000000-0000-4000-8000-000000000001"
SECOND = "10000000-0000-4000-8000-000000000002"


@pytest.mark.parametrize("action", ["create", "roll"])
@pytest.mark.parametrize("explicit", [False, True])
def test_key_mutation_reconnects_to_exact_receipt_and_new_request_changes_key(
    action, explicit
):
    class Peer(FakeClient):
        def __init__(self):
            super().__init__({})
            self.receipts = {}
            self.effects = 0
            self.lose_reply = True

        def request(
            self,
            method,
            path,
            payload=None,
            *,
            extra_headers=None,
            query=None,
            timeout_seconds=None,
        ):
            _validate_generated_request(
                httpx2.Request(
                    method, f"https://control.example{path}", json=payload, params=query
                )
            )
            document = query if query is not None else payload
            assert document is not None
            identity = document["request_id"]
            if identity not in self.receipts:
                self.effects += 1
                self.receipts[identity] = GatewayKeyCreated(
                    name="client", models=[], key=f"sk-generation-{self.effects}"
                ).to_dict()
            if self.lose_reply:
                self.lose_reply = False
                raise OSError("response lost after effect")
            return self.receipts[identity]

    peer = Peer()
    arguments = ["key", action, "client", "--yes"]
    if explicit:
        arguments.extend(["--request-key", FIRST])
    args = cli._parser().parse_args(arguments)
    try:
        run_controller(args, peer, lambda: FIRST)
    except OSError:
        pass
    result = run_controller(args, peer, lambda: SECOND)
    assert result["key"] == peer.receipts[FIRST]["key"]
    assert peer.effects == 1
    fresh = cli._parser().parse_args(["key", action, "client", "--yes"])
    assert run_controller(fresh, peer, lambda: SECOND)["key"] != result["key"]
    assert peer.effects == 2

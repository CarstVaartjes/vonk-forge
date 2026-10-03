"""The lane's own requests stay inside what an older Controller accepts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = """
import json

from vonk_control.fleet_profile_contract import FleetProfileLoadRequest

from scripts.development_slice_client import Client
from tests.acceptance.controller_contract import ControllerContract
import runpy

SparkLifecycle = runpy.run_path("tests/acceptance/test_spark_lifecycle.py")[
    "SparkLifecycle"
]

# An older Controller knows only the fields the model requires; every optional
# field the model has since gained is unknown to it (#1086 sent one as null).
current = FleetProfileLoadRequest.model_json_schema()
required = set(current["required"])
assert required != set(current["properties"]), "no optional field to outrun"
older = {
    **current,
    "properties": {
        name: value for name, value in current["properties"].items() if name in required
    },
}
contract = ControllerContract(
    {
        "paths": {
            "/api/profile/{number}/load": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Body"}
                            }
                        }
                    }
                }
            }
        },
        "components": {"schemas": {"Body": older}},
    },
    label="the previous Controller",
)
sent = []


def transport(method, path, payload, headers, timeout):
    contract.check(method, path, payload)
    sent.append(payload)
    return 202, b'{"id":"1"}'


lifecycle = object.__new__(SparkLifecycle)
lifecycle.control = Client(
    "https://controller.test",
    None,
    timeout=1,
    headers={"X-Test": "1"},
    transport=transport,
)
key = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
lifecycle._load_canary_profile({"plan_digest": "a" * 64}, request_key=key)
assert json.loads(sent[0]) == {"request_key": key}
"""


def test_the_lane_loads_a_profile_with_only_what_an_older_controller_knows() -> None:
    subprocess.run(
        [sys.executable, "-c", SCRIPT],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
    )

"""Exercise canonical API recipe serialization against acceptance closure."""

import subprocess
import sys
from pathlib import Path


def test_canary_load_and_cleanup_submit_their_own_reviewed_plan() -> None:
    repository = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import runpy
from vonk_control.fleet_profile_contract import FleetProfileLoadRequest

Lifecycle = runpy.run_path("tests/acceptance/test_spark_lifecycle.py")["SparkLifecycle"]
requests = []

class Control:
    @staticmethod
    def request(method, path, payload, **kwargs):
        assert (method, path, kwargs) == (
            "POST", "/api/profile/1/load", {"allowed": (202,)}
        )
        requests.append(FleetProfileLoadRequest.model_validate_json(json.dumps(payload)))
        return 202, {"id": str(len(requests))}

run = Lifecycle.__new__(Lifecycle)
run.control = Control()
for digest, key in (
    ("a" * 64, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    ("b" * 64, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
):
    run._load_canary_profile({"plan_digest": digest}, request_key=key)
    assert requests[-1].plan_digest == digest
    assert str(requests[-1].request_key) == key
""",
        ],
        cwd=repository,
        check=True,
    )


def test_acceptance_recipe_closure_uses_the_authoritative_model() -> None:
    repository = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import copy
import json
import os
import runpy
from pathlib import Path
from vonk_control.strict_json import serialize_json_value
from vonk_forge_contracts import RecipeDefinition

root = Path(os.environ["VONK_RECIPE_LIBRARY_ROOT"])
fixture = json.loads((root / "tests/fixtures/canonical-synthetic-canary/recipe.json").read_text())
model = RecipeDefinition.model_validate(fixture)
api = serialize_json_value(model)
assert api != fixture  # The actual Controller omits declared optional None fields.
match = runpy.run_path("tests/acceptance/test_spark_lifecycle.py")["_canonical_recipe_matches"]
assert match(api, fixture)
assert match(fixture, api)
assert match(fixture, fixture)
changed = copy.deepcopy(api)
changed["identity"]["slug"] = "different-canonical-recipe"
assert not match(changed, fixture)
malformed = copy.deepcopy(api)
del malformed["identity"]
assert not match(malformed, fixture)
unknown = copy.deepcopy(api)
unknown["unexpected"] = None
assert not match(unknown, fixture)
assert not match(None, fixture)
""",
        ],
        cwd=repository,
        check=True,
    )

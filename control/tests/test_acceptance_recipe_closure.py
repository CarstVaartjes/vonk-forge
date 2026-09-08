"""Exercise canonical API recipe serialization against acceptance closure."""

import subprocess
import sys
from pathlib import Path


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

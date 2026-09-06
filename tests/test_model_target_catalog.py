import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_platform_has_no_shadow_model_target_or_recipe_library_authority() -> None:
    """Model and Recipe documents come from the managed canonical library."""
    assert not (ROOT / "config/model-targets").exists()
    assert not (ROOT / "config/recipes").exists()
    manifest = json.loads(
        (ROOT / "config/recipe-library-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest == {
        "schema_version": 2,
        "kind": "recipe-library-authority",
        "repository": "https://github.com/CarstVaartjes/vonk-forge-recipes.git",
        "development_ref": "main",
        "production_ref": "approved immutable release tag",
        "validation_workflow": "CarstVaartjes/vonk-forge/.github/workflows/validate-recipe-library.yml",
        "authority": {
            "models": "recipe-library-commit",
            "recipes": "recipe-library-commit",
            "packages": "recipe-library-commit",
            "runtime": "vonk-forge-platform",
        },
        "contents": ["models", "recipes", "packages"],
    }

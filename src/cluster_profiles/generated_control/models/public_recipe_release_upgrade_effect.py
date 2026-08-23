from typing import Literal, cast

PublicRecipeReleaseUpgradeEffect = Literal['metadata-only', 'rebuild', 'reinstall', 'restart']

PUBLIC_RECIPE_RELEASE_UPGRADE_EFFECT_VALUES: set[PublicRecipeReleaseUpgradeEffect] = { 'metadata-only', 'rebuild', 'reinstall', 'restart',  }

def check_public_recipe_release_upgrade_effect(value: str) -> PublicRecipeReleaseUpgradeEffect:
    if value in PUBLIC_RECIPE_RELEASE_UPGRADE_EFFECT_VALUES:
        return cast(PublicRecipeReleaseUpgradeEffect, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_RELEASE_UPGRADE_EFFECT_VALUES!r}")

from typing import Literal, cast

EffectiveSettingsSelectionChangeEffectsAdditionalProperty = Literal['none', 'rebuild', 'reinstall', 'reprepare', 'restart']

EFFECTIVE_SETTINGS_SELECTION_CHANGE_EFFECTS_ADDITIONAL_PROPERTY_VALUES: set[EffectiveSettingsSelectionChangeEffectsAdditionalProperty] = { 'none', 'rebuild', 'reinstall', 'reprepare', 'restart',  }

def check_effective_settings_selection_change_effects_additional_property(value: str) -> EffectiveSettingsSelectionChangeEffectsAdditionalProperty:
    if value in EFFECTIVE_SETTINGS_SELECTION_CHANGE_EFFECTS_ADDITIONAL_PROPERTY_VALUES:
        return cast(EffectiveSettingsSelectionChangeEffectsAdditionalProperty, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {EFFECTIVE_SETTINGS_SELECTION_CHANGE_EFFECTS_ADDITIONAL_PROPERTY_VALUES!r}")

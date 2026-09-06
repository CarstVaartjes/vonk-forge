from typing import Literal, cast

EffectiveSettingsSelectionKind = Literal['embedding', 'generation', 'job']

EFFECTIVE_SETTINGS_SELECTION_KIND_VALUES: set[EffectiveSettingsSelectionKind] = { 'embedding', 'generation', 'job',  }

def check_effective_settings_selection_kind(value: str) -> EffectiveSettingsSelectionKind:
    if value in EFFECTIVE_SETTINGS_SELECTION_KIND_VALUES:
        return cast(EffectiveSettingsSelectionKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {EFFECTIVE_SETTINGS_SELECTION_KIND_VALUES!r}")

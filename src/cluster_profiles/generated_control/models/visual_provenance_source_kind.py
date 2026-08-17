from typing import Literal, cast

VisualProvenanceSourceKind = Literal['fork', 'global', 'local', 'workload_run']

VISUAL_PROVENANCE_SOURCE_KIND_VALUES: set[VisualProvenanceSourceKind] = { 'fork', 'global', 'local', 'workload_run',  }

def check_visual_provenance_source_kind(value: str) -> VisualProvenanceSourceKind:
    if value in VISUAL_PROVENANCE_SOURCE_KIND_VALUES:
        return cast(VisualProvenanceSourceKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {VISUAL_PROVENANCE_SOURCE_KIND_VALUES!r}")

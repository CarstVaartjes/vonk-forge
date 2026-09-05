from typing import Literal, cast

LibraryModelArtifactKind = Literal['http.file', 'huggingface.file', 'oci.artifact']

LIBRARY_MODEL_ARTIFACT_KIND_VALUES: set[LibraryModelArtifactKind] = { 'http.file', 'huggingface.file', 'oci.artifact',  }

def check_library_model_artifact_kind(value: str) -> LibraryModelArtifactKind:
    if value in LIBRARY_MODEL_ARTIFACT_KIND_VALUES:
        return cast(LibraryModelArtifactKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_MODEL_ARTIFACT_KIND_VALUES!r}")

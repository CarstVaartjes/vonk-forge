from typing import Literal, cast

CompiledSecurityMountSource = Literal['inputs', 'model', 'outputs']

COMPILED_SECURITY_MOUNT_SOURCE_VALUES: set[CompiledSecurityMountSource] = { 'inputs', 'model', 'outputs',  }

def check_compiled_security_mount_source(value: str) -> CompiledSecurityMountSource:
    if value in COMPILED_SECURITY_MOUNT_SOURCE_VALUES:
        return cast(CompiledSecurityMountSource, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {COMPILED_SECURITY_MOUNT_SOURCE_VALUES!r}")

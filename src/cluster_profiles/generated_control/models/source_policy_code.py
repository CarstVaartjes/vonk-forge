from typing import Literal

SourcePolicyCode = Literal['compose.capabilities', 'compose.devices', 'compose.host_bind', 'compose.host_namespace', 'compose.invalid', 'compose.privileged', 'compose.service_invalid', 'compose.too_large', 'compose.unconfined', 'compose.volumes_invalid', 'dockerfile.add_forbidden', 'dockerfile.base_placeholder', 'dockerfile.base_unpinned', 'dockerfile.build_privilege', 'dockerfile.copy_base_placeholder', 'dockerfile.copy_base_unpinned', 'dockerfile.copy_invalid', 'dockerfile.copy_path', 'dockerfile.from_missing', 'dockerfile.heredoc_forbidden', 'dockerfile.invalid_utf8', 'dockerfile.missing', 'dockerfile.network_host', 'dockerfile.onbuild_forbidden', 'dockerfile.root_user', 'dockerfile.secret_mount']

SOURCE_POLICY_CODE_VALUES: set[SourcePolicyCode] = { 'compose.capabilities', 'compose.devices', 'compose.host_bind', 'compose.host_namespace', 'compose.invalid', 'compose.privileged', 'compose.service_invalid', 'compose.too_large', 'compose.unconfined', 'compose.volumes_invalid', 'dockerfile.add_forbidden', 'dockerfile.base_placeholder', 'dockerfile.base_unpinned', 'dockerfile.build_privilege', 'dockerfile.copy_base_placeholder', 'dockerfile.copy_base_unpinned', 'dockerfile.copy_invalid', 'dockerfile.copy_path', 'dockerfile.from_missing', 'dockerfile.heredoc_forbidden', 'dockerfile.invalid_utf8', 'dockerfile.missing', 'dockerfile.network_host', 'dockerfile.onbuild_forbidden', 'dockerfile.root_user', 'dockerfile.secret_mount',  }

def check_source_policy_code(value: str) -> SourcePolicyCode:
    if value in SOURCE_POLICY_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {SOURCE_POLICY_CODE_VALUES!r}")

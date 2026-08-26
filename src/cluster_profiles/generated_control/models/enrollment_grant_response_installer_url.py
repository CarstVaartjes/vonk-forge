from typing import Literal, cast

EnrollmentGrantResponseInstallerUrl = Literal['https://install.vonkforge.ai/dev/spark', 'https://install.vonkforge.ai/spark']

ENROLLMENT_GRANT_RESPONSE_INSTALLER_URL_VALUES: set[EnrollmentGrantResponseInstallerUrl] = { 'https://install.vonkforge.ai/dev/spark', 'https://install.vonkforge.ai/spark',  }

def check_enrollment_grant_response_installer_url(value: str) -> EnrollmentGrantResponseInstallerUrl:
    if value in ENROLLMENT_GRANT_RESPONSE_INSTALLER_URL_VALUES:
        return cast(EnrollmentGrantResponseInstallerUrl, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ENROLLMENT_GRANT_RESPONSE_INSTALLER_URL_VALUES!r}")

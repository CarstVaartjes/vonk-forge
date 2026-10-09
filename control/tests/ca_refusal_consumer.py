"""Hosted native provider consumer of a real Go Authority HTTPS fixture.

This executable is invoked by the Go connected test after the locked Python
installation. Its transport is real TLS; ephemeral configuration credentials
are unused because the Go owner supplies the already authenticated wire request.
"""

from __future__ import annotations

import ssl
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx2
from pydantic import ValidationError
from vonk_agent_protocol import CertificateCode, UnknownError
from vonk_control.ca_issuance_contract import (
    CertificateRefusalReply,
    CertificateSignRequest,
)
from vonk_control.step_ca import (
    StepCAError,
    StepCAUnavailable,
    StepCertificateAuthority,
)
from vonk_control.strict_json import StrictJSONModel

from .test_step_ca import _write_material


class _BridgeFixture(StrictJSONModel):
    origin: str
    tls_certificate: str
    body: CertificateSignRequest
    expected: CertificateRefusalReply


def main() -> None:
    try:
        fixture = _BridgeFixture.model_validate_json(sys.stdin.buffer.read())
    except ValidationError:
        raise SystemExit("Go bridge fixture failed canonical validation") from None
    with TemporaryDirectory() as directory:
        material = _write_material(Path(directory))
        context = ssl.create_default_context(cadata=fixture.tls_certificate)
        provider = StepCertificateAuthority(
            ca_url=fixture.origin,
            root_certificate_path=material["root_path"],
            intermediate_certificate_path=material["intermediate_path"],
            provisioner_name="vonk-forge-agent",
            provisioner_kid=material["kid"],
            credential_path=material["credential_path"],
            provisioner_public_jwk_path=material["public_jwk_path"],
            timeout_seconds=5.0,
            transport=httpx2.HTTPTransport(verify=context),
        )
        try:
            provider._request(
                "POST", "/1.0/vonk/sign", fixture.body, accept="application/json"
            )
        except StepCAUnavailable as error:
            # Capacity is an unknown observation; repaired capacity can admit
            # the same authenticated request on the next bounded attempt.
            assert (
                fixture.expected.reason_code == CertificateCode.RESPONSE_UNREPRESENTABLE
            )
            assert isinstance(error.typed_error(), UnknownError)
        except StepCAError as error:
            assert error.reason_code == fixture.expected.reason_code
            assert str(error) == (
                f"CA refused exact certificate request: {fixture.expected.detail}"
            )
        else:
            raise AssertionError("actual refused CA effect was accepted by provider")
        finally:
            provider._client.close()


if __name__ == "__main__":
    main()

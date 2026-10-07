"""An issuance reader must accept the issuer's entire committed reply contract."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
from vonk_control.step_ca import StepCAError

from .test_step_ca import (
    NODE_ID,
    NOW,
    _crl_response,
    _csr,
    _issue,
    _Material,
    _provider,
    _success_response,
)


@pytest.mark.parametrize("reader_bytes", (1024, 65535, 65536))
def test_sign_reader_budget_is_checked_before_any_provider_http(
    tmp_path: Path, reader_bytes: int
) -> None:
    """Catches creating a leaf before discovering its reader cannot accept it."""
    exchanges = []
    material: _Material | None = None

    def transport(request: httpx2.Request) -> httpx2.Response:
        assert material is not None
        return _success_response(request, material, exchanges)

    provider, material = _provider(tmp_path, transport, max_response_bytes=reader_bytes)
    request = _csr()
    if reader_bytes < 65536:
        with pytest.raises(
            StepCAError, match="configured CA sign response reader"
        ) as failure:
            _issue(provider, NODE_ID, request, NOW)
        assert failure.value.reason_code == "certificate.response_unrepresentable"
        assert exchanges == []
    else:
        issued = _issue(provider, NODE_ID, request, NOW)
        assert issued.node_id == NODE_ID
        assert len(exchanges) == 1


def test_crl_reader_keeps_its_independently_configured_limit(tmp_path: Path) -> None:
    """Catches accidentally forcing the issuance budget onto CRL observation."""
    material: _Material | None = None

    def transport(_: httpx2.Request) -> httpx2.Response:
        assert material is not None
        return _crl_response(
            material,
            last_update=NOW - timedelta(minutes=1),
            next_update=NOW + timedelta(minutes=59),
        )

    provider, material = _provider(tmp_path, transport, max_response_bytes=1024)
    assert provider.revocation_bundle(NOW).startswith(b"-----BEGIN X509 CRL-----")

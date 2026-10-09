"""An issuance reader must accept the issuer's entire committed reply contract."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx2
import pytest

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
def test_sign_reader_budget_repairs_local_configuration(
    tmp_path: Path, reader_bytes: int
) -> None:
    """Catches refusing valid issuance because the local reader is undersized."""
    exchanges = []
    material: _Material | None = None

    def transport(request: httpx2.Request) -> httpx2.Response:
        assert material is not None
        return _success_response(request, material, exchanges)

    provider, material = _provider(tmp_path, transport, max_response_bytes=reader_bytes)
    request = _csr()
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
    bundle = provider.revocation_bundle(NOW)
    assert isinstance(bundle, bytes)
    assert bundle.startswith(b"-----BEGIN X509 CRL-----")


def test_largest_supported_response_metadata_fits_before_provider_effect(tmp_path):
    """Catches a legal generation rejected by a parallel metadata capacity gate."""
    material: _Material | None = None
    exchanges = []

    def transport(request):
        assert material is not None
        return _success_response(request, material, exchanges)

    provider, material = _provider(tmp_path, transport, max_response_bytes=1024)
    csr = _csr()
    request = provider.prepare_request(
        NODE_ID,
        csr,
        NOW,
        purpose="enrollment",
        source_serial=None,
        generation=2**31 - 1,
    )
    issued = provider.issue_node(NODE_ID, csr, NOW, request=request)
    assert issued.node_id == NODE_ID
    assert len(exchanges) == 1

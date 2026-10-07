"""Explicit fixed issuer-policy fixture for Controller-only enrollment tests."""

import hashlib
import secrets
import threading
from datetime import datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from vonk_control.ca_issuance_contract import CertificateIssuanceBinding
from vonk_control.pki import CertificateAuthority, IssuedCertificate
from vonk_control.step_ca import StepCAIssuancePending


class FixtureCertificateAuthority(CertificateAuthority):
    def _begin(self, request: CertificateIssuanceBinding) -> None:
        if not hasattr(self, "_journal_guard"):
            self._journal_guard = threading.Lock()
            self._journal = {}
        with self._journal_guard:
            if request.request_id in self._journal:
                raise StepCAIssuancePending("certificate.issuance_in_progress")
            self._journal[request.request_id] = None

    def _finish(
        self, request: CertificateIssuanceBinding, issued: IssuedCertificate
    ) -> IssuedCertificate:
        with self._journal_guard:
            self._journal[request.request_id] = issued
        return issued

    def prepare_request(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        purpose: str,
        source_serial: str | None,
        generation: int,
    ) -> CertificateIssuanceBinding:
        csr = x509.load_pem_x509_csr(csr_pem)
        return CertificateIssuanceBinding.model_validate(
            {
                "request_id": secrets.token_urlsafe(32),
                "node_id": node_id,
                "csr_sha256": hashlib.sha256(
                    csr.public_bytes(serialization.Encoding.DER)
                ).hexdigest(),
                "serial": str(getattr(self, "_serial", 0) + 1),
                "not_before": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "not_after": (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "issuer_fingerprint": "a" * 64,
                "provisioner_name": "fixture",
                "provisioner_kid": "fixture",
                "policy_sha256": "b" * 64,
                "purpose": purpose,
                "source_serial": source_serial,
                "generation": generation,
            }
        )

    def observe_node(
        self,
        csr_pem: bytes,
        now: datetime,
        *,
        request: CertificateIssuanceBinding,
    ) -> IssuedCertificate | None:
        if not hasattr(self, "_journal_guard"):
            return None
        with self._journal_guard:
            if request.request_id not in self._journal:
                return None
            issued = self._journal[request.request_id]
            if issued is None:
                raise StepCAIssuancePending("certificate.issuance_in_progress")
            return issued

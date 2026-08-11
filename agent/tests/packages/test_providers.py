from __future__ import annotations

from dataclasses import replace

import pytest
from vonk_agent.packages.providers import (
    Credential,
    CredentialError,
    FetchResponse,
    GitSnapshotProvider,
    HuggingFaceProvider,
    NetworkHop,
    OCIFetchProvider,
    ProviderError,
    ProviderRegistry,
    PythonArtifactProvider,
    SignedIndexProvider,
    SourceLocation,
    SourcePolicy,
    SourcePolicyError,
    Validators,
    VerifiedHTTPSProvider,
)

SHA_A = "a" * 64


class Transport:
    def __init__(self, response: FetchResponse) -> None:
        self.response = response
        self.requests = []

    def open(self, request, _deadline):
        self.requests.append(request)
        return self.response


def _response(
    *,
    url: str = "https://models.example.test/model.bin",
    addresses: tuple[str, ...] = ("8.8.8.8",),
    status: int = 200,
    start: int = 0,
    etag: str = '"immutable-a"',
) -> FetchResponse:
    return FetchResponse(
        status_code=status,
        start_offset=start,
        total_size=4,
        validators=Validators(etag=etag),
        chunks=(b"data",),
        hops=(NetworkHop(url, addresses),),
    )


def _source(provider: str = "https") -> SourceLocation:
    return SourceLocation(
        provider=provider,
        url="https://models.example.test/model.bin",
        immutable_id=f"sha256:{SHA_A}",
        allowed_domains=("models.example.test",),
        credential_ref="model-read-token",
    )


def test_https_provider_sends_range_and_if_range_without_persisting_credentials() -> (
    None
):
    transport = Transport(_response(status=206, start=2))
    resolutions = 0

    def credential(_reference: str) -> Credential:
        nonlocal resolutions
        resolutions += 1
        return Credential("Bearer secret-value", expires_at=100.0)

    provider = VerifiedHTTPSProvider(
        transport,
        policy=SourcePolicy(),
        credentials=credential,
        monotonic=lambda: 1.0,
    )
    stream = provider.open(
        _source(),
        offset=2,
        validators=Validators(etag='"immutable-a"'),
        deadline=None,
    )

    request = transport.requests[-1]
    assert stream.start_offset == 2
    assert request.headers["Range"] == "bytes=2-"
    assert request.headers["If-Range"] == '"immutable-a"'
    assert request.authorization == "Bearer secret-value"
    assert "secret-value" not in repr(request)
    assert "secret-value" not in repr(_source())
    assert resolutions == 1


def test_provider_resolves_fresh_credentials_for_every_request_and_rejects_expiry() -> (
    None
):
    transport = Transport(_response())
    provider = VerifiedHTTPSProvider(
        transport,
        policy=SourcePolicy(),
        credentials=lambda _reference: Credential("token", expires_at=5.0),
        monotonic=lambda: 5.0,
    )

    with pytest.raises(CredentialError, match="credential is unavailable") as caught:
        provider.open(_source(), 0, Validators(), None)

    assert "token" not in str(caught.value)
    assert transport.requests == []


@pytest.mark.parametrize(
    ("url", "addresses", "message"),
    (
        ("https://evil.example.test/model.bin", ("8.8.8.8",), "domain"),
        ("https://models.example.test/model.bin", ("127.0.0.1",), "address"),
        ("https://models.example.test/model.bin", ("169.254.1.2",), "address"),
    ),
)
def test_every_redirect_and_resolved_address_is_policy_checked(
    url: str,
    addresses: tuple[str, ...],
    message: str,
) -> None:
    transport = Transport(_response(url=url, addresses=addresses))
    provider = VerifiedHTTPSProvider(transport, policy=SourcePolicy())

    with pytest.raises(SourcePolicyError, match=message):
        provider.open(replace(_source(), credential_ref=None), 0, Validators(), None)


def test_redirect_count_is_bounded() -> None:
    hops = tuple(
        NetworkHop(f"https://models.example.test/{index}", ("8.8.8.8",))
        for index in range(7)
    )
    response = replace(_response(), hops=hops)
    provider = VerifiedHTTPSProvider(
        Transport(response), policy=SourcePolicy(max_redirects=2)
    )

    with pytest.raises(SourcePolicyError, match="redirect"):
        provider.open(replace(_source(), credential_ref=None), 0, Validators(), None)


@pytest.mark.parametrize(
    "url",
    (
        "http://models.example.test/model.bin",
        "https://user:secret@models.example.test/model.bin",
        "https://models.example.test/model.bin?token=secret",
    ),
)
def test_source_location_rejects_unverified_or_credential_bearing_urls(
    url: str,
) -> None:
    with pytest.raises(SourcePolicyError, match="source URL"):
        SourceLocation(
            provider="https",
            url=url,
            immutable_id=f"sha256:{SHA_A}",
            allowed_domains=("models.example.test",),
        )


def test_generic_provider_registry_covers_immutable_protocols() -> None:
    delegate = VerifiedHTTPSProvider(Transport(_response()), policy=SourcePolicy())
    registry = ProviderRegistry(
        (
            delegate,
            GitSnapshotProvider(delegate),
            OCIFetchProvider(delegate),
            HuggingFaceProvider(delegate),
            PythonArtifactProvider(delegate),
            SignedIndexProvider(delegate),
        )
    )

    assert registry.provider("https") is delegate
    assert registry.provider("git").name == "git"
    assert registry.provider("oci").name == "oci"
    assert registry.provider("huggingface").name == "huggingface"
    assert registry.provider("python-index").name == "python-index"
    assert registry.provider("signed-http-index").name == "signed-http-index"
    with pytest.raises(ProviderError, match="unsupported"):
        registry.provider("future-protocol")


@pytest.mark.parametrize(
    ("provider", "immutable_id"),
    (
        ("git", "main"),
        ("git", "abc123"),
        ("oci", "latest"),
        ("huggingface", "main"),
        ("python-index", "package==1.0"),
        ("signed-http-index", f"sha256:{SHA_A}"),
    ),
)
def test_provider_specific_sources_require_full_immutable_identity(
    provider: str,
    immutable_id: str,
) -> None:
    delegate = VerifiedHTTPSProvider(Transport(_response()), policy=SourcePolicy())
    typed = {
        "git": GitSnapshotProvider(delegate),
        "oci": OCIFetchProvider(delegate),
        "huggingface": HuggingFaceProvider(delegate),
        "python-index": PythonArtifactProvider(delegate),
        "signed-http-index": SignedIndexProvider(delegate),
    }[provider]
    source = replace(
        _source(),
        provider=provider,
        immutable_id=immutable_id,
        credential_ref=None,
    )

    with pytest.raises(ProviderError, match="immutable identity"):
        typed.open(source, 0, Validators(), None)

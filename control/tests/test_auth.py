import pytest
from vonk_control.auth import Actor, AuthError, TokenCodec
from vonk_control.development_tokens import issue_development_admin_token


def test_signed_token_round_trip_and_tamper_rejection() -> None:
    codec = TokenCodec(b"x" * 32)
    token = codec.issue(Actor("admin", "administrator"), ttl_seconds=60, now=100)
    assert codec.verify(token, now=120) == Actor("admin", "administrator")
    with pytest.raises(AuthError):
        codec.verify(token + "changed", now=120)
    with pytest.raises(AuthError, match="expired"):
        codec.verify(token, now=161)


def test_codec_rejects_short_signing_key() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        TokenCodec(b"short")


def test_dependency_free_development_issuer_matches_control_verifier() -> None:
    signing_key = b"runtime-generated-development-key"
    token = issue_development_admin_token(
        signing_key=signing_key,
        ttl_seconds=60,
        now=100,
    )

    assert TokenCodec(signing_key).verify(token, now=101) == Actor(
        "development-operator", "administrator"
    )


def test_dependency_free_development_issuer_rejects_short_signing_key() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        issue_development_admin_token(
            signing_key=b"short",
            ttl_seconds=60,
            now=100,
        )

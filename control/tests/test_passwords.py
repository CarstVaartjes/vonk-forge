import pytest
from vonk_control.passwords import (
    PasswordVerification,
    hash_password,
    verify_password,
)


def test_password_verifier_authenticates_only_the_original_secret() -> None:
    verifier = hash_password("A" * 43)
    assert verifier != hash_password("A" * 43)  # independently salted
    assert verify_password(verifier, "B" * 43) == PasswordVerification(False, False)
    assert verify_password(verifier, "A" * 43) == PasswordVerification(True, False)


@pytest.mark.parametrize("password", ["", "x" * 257])
def test_password_boundary_rejects_empty_or_oversized_input(password: str) -> None:
    with pytest.raises(Exception) as _ending:
        hash_password(password)
    fresh = hash_password("valid password")
    assert verify_password(fresh, "valid password").valid


def test_verify_password_returns_one_generic_invalid_result() -> None:
    assert verify_password("not-a-phc-string", "wrong") == PasswordVerification(
        False, False
    )

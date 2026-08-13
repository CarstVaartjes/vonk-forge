from base64 import b64decode

import pytest
from vonk_control.passwords import (
    PasswordPolicyError,
    PasswordVerification,
    hash_password,
    verify_password,
)


def test_hash_password_emits_the_exact_argon2id_policy() -> None:
    verifier = hash_password("A" * 43)
    assert verifier.startswith("$argon2id$v=19$m=65536,t=3,p=1$")
    _, _, _, _, encoded_salt, encoded_hash = verifier.split("$")
    assert len(b64decode(encoded_salt + "=" * (-len(encoded_salt) % 4))) == 16
    assert len(b64decode(encoded_hash + "=" * (-len(encoded_hash) % 4))) == 32
    assert verify_password(verifier, "A" * 43) == PasswordVerification(True, False)


@pytest.mark.parametrize("password", ["", "x" * 257])
def test_password_boundary_rejects_empty_or_oversized_input(password: str) -> None:
    with pytest.raises(PasswordPolicyError, match="password is invalid"):
        hash_password(password)


def test_verify_password_returns_one_generic_invalid_result() -> None:
    assert verify_password("not-a-phc-string", "wrong") == PasswordVerification(False, False)

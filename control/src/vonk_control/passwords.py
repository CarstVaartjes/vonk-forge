from dataclasses import dataclass

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError


class PasswordPolicyError(ValueError):
    pass


HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65_536,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


@dataclass(frozen=True)
class PasswordVerification:
    valid: bool
    needs_rehash: bool


def _bounded(password: str) -> str:
    if not isinstance(password, str) or not 1 <= len(password.encode("utf-8")) <= 256:
        raise PasswordPolicyError("password is invalid")
    return password


def hash_password(password: str) -> str:
    return HASHER.hash(_bounded(password))


def verify_password(verifier: str, password: str) -> PasswordVerification:
    try:
        bounded = _bounded(password)
        valid = HASHER.verify(verifier, bounded)
        return PasswordVerification(valid, valid and HASHER.check_needs_rehash(verifier))
    except (PasswordPolicyError, InvalidHashError, VerificationError):
        return PasswordVerification(False, False)

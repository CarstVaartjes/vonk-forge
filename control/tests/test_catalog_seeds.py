from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from vonk_control.catalog_seeds import seed_standard_families
from vonk_control.models import Base, PackageFamily


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as value:
        yield value


def test_standard_seed_is_idempotent_and_preserves_user_edits(session) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    first = seed_standard_families(session, now)
    session.commit()
    session.get(PackageFamily, "vllm").display_name = "My vLLM"
    session.commit()

    second = seed_standard_families(session, now)
    session.commit()

    assert first.created == 5
    assert second.created == 0
    assert session.get(PackageFamily, "vllm").display_name == "My vLLM"


def test_standard_seeds_are_typed_arm64_definitions(session) -> None:
    seed_standard_families(session, datetime.now(UTC))
    session.commit()

    assert set(session.scalars(select(PackageFamily.id))) == {
        "oci",
        "huggingface-snapshot",
        "vllm",
        "sglang",
        "llama-cpp",
    }
    for family in session.scalars(select(PackageFamily)):
        assert family.builtin is True
        assert family.schema_version == 1
        assert family.definition["architecture"] == "linux/arm64"
        assert family.definition["capability"].endswith(".v1")


def test_seed_participates_in_the_callers_transaction(session) -> None:
    seed_standard_families(session, datetime.now(UTC))
    session.rollback()

    assert session.get(PackageFamily, "vllm") is None

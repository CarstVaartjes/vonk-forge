"""A busy admission lock names the work that holds it."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from vonk_control.admission_locking import (
    AdmissionLockBusy,
    acquire_admission_keys,
    node_admission_key,
)

NODE = "spk_" + "a" * 32


def test_busy_node_key_names_the_labelled_holder(postgres_engine) -> None:
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as holder:
        acquire_admission_keys(
            holder, (node_admission_key(NODE),), holder="recipe-operation"
        )
        with sessions.begin() as taker, pytest.raises(AdmissionLockBusy) as busy:
            acquire_admission_keys(
                taker, (node_admission_key(NODE),), holder="profile-admission"
            )
    assert busy.value.holder == "recipe-operation"
    assert "held by recipe-operation" in str(busy.value)
    with sessions.begin() as fresh:
        acquire_admission_keys(fresh, (node_admission_key(NODE),))


def test_unlabelled_busy_node_key_clears_for_a_fresh_admission(
    postgres_engine,
) -> None:
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as holder:
        # A pooled connection can retain an application name from earlier work.
        # Establish the unlabelled boundary explicitly for this transaction.
        holder.execute(text("SELECT set_config('application_name', '', true)"))
        acquire_admission_keys(holder, (node_admission_key(NODE),))
        with sessions.begin() as taker, pytest.raises(AdmissionLockBusy) as busy:
            acquire_admission_keys(taker, (node_admission_key(NODE),))
    assert busy.value.holder is None
    assert "held by" not in str(busy.value)
    with sessions.begin() as fresh:
        acquire_admission_keys(fresh, (node_admission_key(NODE),))


def test_the_label_ends_with_the_transaction(postgres_engine) -> None:
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as first:
        acquire_admission_keys(
            first, (node_admission_key(NODE),), holder="recipe-operation"
        )
    with sessions.begin() as second:
        acquire_admission_keys(
            second, (node_admission_key(NODE),), holder="profile-admission"
        )

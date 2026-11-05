"""Serialization of user-authority *writers* (the administrator bootstrap).

Readers (profile and recipe-update mutations) take no lock: nothing at runtime
writes ``users``, so a plain read of the acting user is already current.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session


def serialize_user_authority(session: Session) -> None:
    """Serialize PostgreSQL user-authority writers, including absent rows."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))

"""Serialization boundary for mutations to user authority."""

from sqlalchemy import text
from sqlalchemy.orm import Session


def serialize_user_authority(session: Session) -> None:
    """Serialize PostgreSQL user-authority writers, including absent rows."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))

"""Database engine and session construction."""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def build_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    from .fleet_events import FleetEventRecorder

    sessions = sessionmaker(engine, expire_on_commit=False)
    FleetEventRecorder.install(sessions)
    return sessions

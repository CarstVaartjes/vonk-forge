"""Short consistent observation transactions, always closed before streaming."""

import sqlite3

from sqlalchemy import text
from sqlalchemy.orm import Session


def begin_observation_capture(session: Session) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    else:
        connection = session.connection()
        driver = connection.connection.driver_connection
        # sqlite's legacy transaction mode does not BEGIN for a SELECT. Without
        # an explicit read transaction, independent SELECTs can mix generations.
        if isinstance(driver, sqlite3.Connection) and not driver.in_transaction:
            session.execute(text("BEGIN"))

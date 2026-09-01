from alembic import context
from sqlalchemy import engine_from_config, pool
from vonk_control.models import Base

config = context.config

_RETAINED_LEGACY_TABLE = "agent_upgrade_compatibility_recoveries"


def _include_object(
    object_: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    del compare_to
    if not reflected:
        return True
    if type_ == "table":
        return name != _RETAINED_LEGACY_TABLE
    if type_ == "index":
        table = getattr(object_, "table", None)
        return getattr(table, "name", None) != _RETAINED_LEGACY_TABLE
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=Base.metadata,
        literal_binds=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()

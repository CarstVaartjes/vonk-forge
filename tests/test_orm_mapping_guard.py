"""Keep the pre-split ORM graph and flush ordering without consulting Git."""

from importlib import import_module
from inspect import getmembers, isclass
from pathlib import Path
from pkgutil import walk_packages

from sqlalchemy import Table, inspect
from sqlalchemy.orm import configure_mappers
from vonk_control import models

SNAPSHOT = Path(__file__).resolve().parents[1] / "tools/orm-mapping-snapshot.txt"


def test_all_models_share_metadata_and_resolve_foreign_keys() -> None:
    # Discover implementations too: a second Base hidden by the public facade
    # must not escape this guard merely because it has its own mapper registry.
    modules = [models]
    modules.extend(
        import_module(info.name)
        for info in walk_packages(models.__path__, models.__name__ + ".")
    )
    for module in modules:
        for _, model in getmembers(module, isclass):
            mapper = inspect(model, raiseerr=False)
            if mapper is not None and hasattr(mapper, "local_table"):
                assert mapper.registry is models.Base.registry
                assert mapper.local_table.metadata is models.Base.metadata
    for table in models.Base.metadata.tables.values():
        for foreign_key in table.foreign_keys:
            target = foreign_key.column.table
            assert target is models.Base.metadata.tables[target.key]


def test_mapper_graph_matches_pre_split_snapshot() -> None:
    # SQLAlchemy sorts independent mappers by module + class name during flush.
    # A package move can therefore reorder inserts even with identical tables
    # and no relationships. Pin mapper ordering alongside the relationship set.
    configure_mappers()
    actual = []
    for mapper in models.Base.registry.mappers:
        table = mapper.local_table
        assert isinstance(table, Table)
        relationships = sorted(
            f"{rel.key}:{rel.mapper.class_.__module__}.{rel.mapper.class_.__name__}"
            f":{rel.back_populates}:{rel.direction.name}"
            for rel in mapper.relationships
        )
        actual.append(f"{mapper._sort_key}|{table.name}|" + ",".join(relationships))
    assert sorted(actual) == SNAPSHOT.read_text().splitlines()

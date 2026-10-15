"""Idempotent Alembic operations.

Production databases predate Alembic and were built by ``init_db`` and
``scripts/migrate_schema.py``. Every revision must therefore be safe to run
against a schema that already has some or all of its objects.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import sqlalchemy as sa
from alembic import op


_inspectors: dict[int, sa.engine.Inspector] = {}


def _inspector() -> sa.engine.Inspector:
    bind = op.get_bind()
    insp = _inspectors.get(id(bind))
    if insp is None:
        insp = sa.inspect(bind)
        _inspectors[id(bind)] = insp
    return insp


def _changed() -> None:
    insp = _inspectors.get(id(op.get_bind()))
    if insp is not None:
        insp.clear_cache()


def is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def has_table(table: str) -> bool:
    return _inspector().has_table(table)


def has_column(table: str, column: str) -> bool:
    if not has_table(table):
        return False
    return any(col["name"] == column for col in _inspector().get_columns(table))


def has_index(table: str, index: str) -> bool:
    if not has_table(table):
        return False
    return any(ix["name"] == index for ix in _inspector().get_indexes(table))


def add_column_if_missing(table: str, column: sa.Column) -> None:
    if has_table(table) and not has_column(table, column.name):
        op.add_column(table, column)
        _changed()


def add_columns_if_missing(table: str, columns: Iterable[sa.Column]) -> None:
    for column in columns:
        add_column_if_missing(table, column)


def create_index_if_missing(index: str, table: str, columns: Sequence[str], *, unique: bool = False) -> None:
    if has_table(table) and not has_index(table, index):
        op.create_index(index, table, list(columns), unique=unique)
        _changed()


def create_table_if_missing(table: str, *columns: sa.SchemaItem, indexes: Sequence[tuple[str, Sequence[str]]] = ()) -> None:
    if not has_table(table):
        op.create_table(table, *columns)
        _changed()
    for name, cols in indexes:
        create_index_if_missing(name, table, cols)


def has_unique_constraint(table: str, name: str) -> bool:
    if not has_table(table):
        return False
    return any(uc.get("name") == name for uc in _inspector().get_unique_constraints(table))


def drop_unique_if_present(table: str, name: str) -> None:
    """SQLite cannot drop a constraint in place, so it rebuilds the table in batch mode."""
    if not has_unique_constraint(table, name):
        return
    if is_postgres():
        op.drop_constraint(name, table, type_="unique")
    else:
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.drop_constraint(name, type_="unique")
    _changed()


def create_partial_index_if_missing(
    index: str, table: str, columns: Sequence[str], where: str, *, unique: bool = False
) -> None:
    if has_table(table) and not has_index(table, index):
        clause = sa.text(where)
        op.create_index(index, table, list(columns), unique=unique, postgresql_where=clause, sqlite_where=clause)
        _changed()


def drop_column_if_present(table: str, column: str) -> None:
    if has_column(table, column):
        op.drop_column(table, column)
        _changed()


def drop_table_if_present(table: str) -> None:
    if has_table(table):
        op.drop_table(table)
        _changed()

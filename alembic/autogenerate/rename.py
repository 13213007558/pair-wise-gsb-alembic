# mypy: allow-untyped-defs, allow-incomplete-defs, allow-untyped-calls

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import TYPE_CHECKING

from sqlalchemy import Column
from sqlalchemy import MetaData
from sqlalchemy import Table

from .. import util

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import ReflectedForeignKeyConstraint
    from sqlalchemy.engine.interfaces import ReflectedIndex
    from sqlalchemy.engine.interfaces import ReflectedUniqueConstraint


class RenameMap:
    """Parse and validate the ``rename_map`` autogenerate parameter.

    The parameter is a dictionary with optional ``"tables"`` and
    ``"columns"`` keys.  Values may be mappings or sequences of
    ``(key, value)`` pairs::

        rename_map = {
            "tables": {
                "old_table": "new_table",
                ("myschema", "other_old"): "other_new",
            },
            "columns": {
                ("customer", "full_name"): "display_name",
                ("myschema", "customer", "age"): "years",
            },
        }

    Table keys are either a plain table name (default schema) or a
    ``(schema, table_name)`` tuple.  Column keys are either
    ``(table_name, column_name)`` or
    ``(schema, table_name, column_name)`` tuples.  The table name
    within a column key may be given either as the existing
    (database-side) name or, if the table itself is also renamed, the new
    name; it is resolved against the table rename map.

    """

    def __init__(
        self,
        table_renames: dict[tuple[str | None, str], str],
        column_renames: dict[tuple[str | None, str, str], str],
    ) -> None:
        # (schema, old_table_name) -> new_table_name
        self.table_renames = table_renames
        # (schema, table_name_as_given, old_column_name) -> new_column_name
        self.column_renames = column_renames

    @classmethod
    def from_parameter(cls, param: Any) -> RenameMap | None:
        if param is None:
            return None
        if not isinstance(param, Mapping):
            raise util.CommandError(
                "rename_map must be a mapping with optional 'tables' and "
                "'columns' keys"
            )
        unknown = set(param).difference(["tables", "columns"])
        if unknown:
            raise util.CommandError(
                "rename_map got unexpected top-level keys: %s; expected "
                "only 'tables' and/or 'columns'"
                % ", ".join(sorted(repr(k) for k in unknown))
            )
        table_renames = _normalize_pairs(
            param.get("tables"), "tables", _table_key
        )
        column_renames = _normalize_pairs(
            param.get("columns"), "columns", _column_key
        )
        return cls(table_renames, column_renames)

    def resolve(
        self,
        conn_table_names: set[tuple[str | None, str]],
        metadata_table_names: set[tuple[str | None, str]],
    ) -> ResolvedRenames:
        """Validate this map against the table names present in the
        database and in the target metadata, and return a
        :class:`.ResolvedRenames` object used for the comparison.

        Raises :class:`~alembic.util.CommandError` for any mapping entry
        that refers to a source object that does not exist, a target name
        that is already in use by an unrelated object, or that forms a
        circular rename.

        """
        _check_cycles(
            [
                ((schema, old), (schema, new))
                for (schema, old), new in self.table_renames.items()
            ],
            "table",
        )

        seen_targets: set[tuple[str | None, str]] = set()
        table_renames: dict[tuple[str | None, str], str] = {}
        for (schema, old), new in self.table_renames.items():
            entry = _format_entry((schema, old), new)
            if (schema, old) not in conn_table_names:
                raise util.CommandError(
                    "rename_map entry %s refers to source table %r which "
                    "was not found in the database"
                    % (entry, _format_name(schema, old))
                )
            if (schema, new) in conn_table_names:
                raise util.CommandError(
                    "rename_map entry %s refers to target table %r which "
                    "is already present in the database as an unrelated "
                    "object" % (entry, _format_name(schema, new))
                )
            if (schema, new) not in metadata_table_names:
                raise util.CommandError(
                    "rename_map entry %s refers to target table %r which "
                    "is not present in the target metadata"
                    % (entry, _format_name(schema, new))
                )
            if (schema, new) in seen_targets:
                raise util.CommandError(
                    "rename_map entry %s maps more than one source to "
                    "target table %r" % (entry, _format_name(schema, new))
                )
            seen_targets.add((schema, new))
            table_renames[(schema, old)] = new

        table_inverse = {
            (schema, new): old for (schema, old), new in table_renames.items()
        }

        column_renames: dict[tuple[str | None, str], dict[str, str]] = {}
        for (schema, table_key, old), new in self.column_renames.items():
            entry = _format_entry((schema, table_key, old), new)
            if (schema, table_key) in table_renames:
                conn_tname = table_key
            elif (schema, table_key) in table_inverse:
                conn_tname = table_inverse[(schema, table_key)]
            elif (schema, table_key) in conn_table_names:
                conn_tname = table_key
            else:
                raise util.CommandError(
                    "rename_map entry %s refers to source table %r which "
                    "was not found in the database"
                    % (entry, _format_name(schema, table_key))
                )
            meta_tname = table_renames.get((schema, conn_tname), conn_tname)
            if (schema, meta_tname) not in metadata_table_names:
                raise util.CommandError(
                    "rename_map entry %s refers to table %r which is not "
                    "present in the target metadata"
                    % (entry, _format_name(schema, meta_tname))
                )
            per_table = column_renames.setdefault((schema, conn_tname), {})
            if old in per_table:
                raise util.CommandError(
                    "rename_map entry %s maps source column %r more than "
                    "once" % (entry, old)
                )
            per_table[old] = new

        for (schema, conn_tname), colmap in column_renames.items():
            _check_cycles(
                [
                    ((schema, conn_tname, old), (schema, conn_tname, new))
                    for old, new in colmap.items()
                ],
                "column",
            )
            targets: set[str] = set()
            for new in colmap.values():
                if new in targets:
                    raise util.CommandError(
                        "rename_map maps more than one source column to "
                        "target column %r on table %r"
                        % (new, _format_name(schema, conn_tname))
                    )
                targets.add(new)

        return ResolvedRenames(table_renames, column_renames)


class ResolvedRenames:
    """Holds the validated rename mapping, resolved so that all keys
    use database-side (source) names."""

    def __init__(
        self,
        table_renames: dict[tuple[str | None, str], str],
        column_renames: dict[tuple[str | None, str], dict[str, str]],
    ) -> None:
        # (schema, conn_table_name) -> metadata_table_name
        self.table_renames = table_renames
        # (schema, conn_table_name) -> {conn_column_name: metadata_name}
        self.column_renames = column_renames
        self._table_inverse = {
            (schema, new): old
            for (schema, old), new in table_renames.items()
        }

    def table_target(self, schema: str | None, conn_tname: str) -> str:
        """Return the metadata-side name for a database-side table name."""
        return self.table_renames.get((schema, conn_tname), conn_tname)

    def table_source(self, schema: str | None, meta_tname: str) -> str:
        """Return the database-side name for a metadata-side table name."""
        return self._table_inverse.get((schema, meta_tname), meta_tname)

    def column_renames_for(
        self, schema: str | None, conn_tname: str
    ) -> dict[str, str]:
        return self.column_renames.get((schema, conn_tname), {})

    def translate_column_name(
        self, schema: str | None, conn_tname: str, name: str
    ) -> str:
        return self.column_renames.get((schema, conn_tname), {}).get(
            name, name
        )

    def translate_index_params(
        self, schema: str | None, tname: str, params: ReflectedIndex
    ) -> ReflectedIndex:
        colmap = self.column_renames.get((schema, tname))
        if not colmap:
            return params
        params = dict(params)  # type: ignore[assignment]
        params["column_names"] = [
            colmap.get(c, c) if c is not None else None
            for c in params["column_names"]
        ]
        sorting = params.get("column_sorting")
        if sorting:
            params["column_sorting"] = {
                colmap.get(c, c): v for c, v in sorting.items()
            }
        return params

    def translate_unique_params(
        self,
        schema: str | None,
        tname: str,
        params: ReflectedUniqueConstraint,
    ) -> ReflectedUniqueConstraint:
        colmap = self.column_renames.get((schema, tname))
        if not colmap:
            return params
        params = dict(params)  # type: ignore[assignment]
        params["column_names"] = [
            colmap.get(c, c) for c in params["column_names"]
        ]
        return params

    def translate_fk_params(
        self,
        schema: str | None,
        tname: str,
        params: ReflectedForeignKeyConstraint,
    ) -> ReflectedForeignKeyConstraint:
        params = dict(params)  # type: ignore[assignment]
        colmap = self.column_renames.get((schema, tname), {})
        if colmap:
            params["constrained_columns"] = [
                colmap.get(c, c) for c in params["constrained_columns"]
            ]
        ref_schema = params.get("referred_schema")
        ref_table = params["referred_table"]
        params["referred_table"] = self.table_renames.get(
            (ref_schema, ref_table), ref_table
        )
        ref_colmap = self.column_renames.get((ref_schema, ref_table), {})
        if ref_colmap:
            params["referred_columns"] = [
                ref_colmap.get(c, c) for c in params["referred_columns"]
            ]
        return params

    def translate_index_params(
        self, schema: str | None, tname: str, params: ReflectedIndex
    ) -> ReflectedIndex:
        colmap = self.column_renames.get((schema, tname))
        if not colmap:
            return params
        params = dict(params)  # type: ignore[assignment]
        params["column_names"] = [
            colmap.get(c, c) if c is not None else None
            for c in params["column_names"]
        ]
        sorting = params.get("column_sorting")
        if sorting:
            params["column_sorting"] = {
                colmap.get(c, c): v for c, v in sorting.items()
            }
        return params

    def translate_unique_params(
        self,
        schema: str | None,
        tname: str,
        params: ReflectedUniqueConstraint,
    ) -> ReflectedUniqueConstraint:
        colmap = self.column_renames.get((schema, tname))
        if not colmap:
            return params
        params = dict(params)  # type: ignore[assignment]
        params["column_names"] = [
            colmap.get(c, c) for c in params["column_names"]
        ]
        return params

    def translate_fk_params(
        self,
        schema: str | None,
        tname: str,
        params: ReflectedForeignKeyConstraint,
    ) -> ReflectedForeignKeyConstraint:
        params = dict(params)  # type: ignore[assignment]
        colmap = self.column_renames.get((schema, tname), {})
        if colmap:
            params["constrained_columns"] = [
                colmap.get(c, c) for c in params["constrained_columns"]
            ]
        ref_schema = params.get("referred_schema")
        ref_table = params["referred_table"]
        params["referred_table"] = self.table_renames.get(
            (ref_schema, ref_table), ref_table
        )
        ref_colmap = self.column_renames.get((ref_schema, ref_table), {})
        if ref_colmap:
            params["referred_columns"] = [
                ref_colmap.get(c, c) for c in params["referred_columns"]
            ]
        return params

    def translate_conn_table(
        self, conn_table: Table, new_tname: str
    ) -> Table:
        """Return a copy of a reflected table in which renamed columns
        (and the table itself, if renamed) use their metadata-side names.

        This copy is used only for constraint/index comparison, so that
        constraints that refer to renamed columns are matched correctly
        against the metadata.

        """
        colmap = self.column_renames.get(
            (conn_table.schema, conn_table.name), {}
        )
        new_metadata = MetaData()
        new_table = Table(
            new_tname,
            new_metadata,
            schema=conn_table.schema,
            comment=conn_table.comment,
        )
        for col in conn_table.c:
            new_table.append_column(
                Column(
                    colmap.get(col.name, col.name),
                    col.type,
                    nullable=col.nullable,
                    server_default=col.server_default,
                    comment=col.comment,
                    primary_key=col.primary_key,
                    autoincrement=col.autoincrement,
                )
            )
        return new_table


class ResolvedRenames:
    """Holds the validated rename mapping, resolved so that all keys
    use database-side (source) names."""

    def __init__(
        self,
        table_renames: dict[tuple[str | None, str], str],
        column_renames: dict[tuple[str | None, str], dict[str, str]],
    ) -> None:
        # (schema, conn_table_name) -> metadata_table_name
        self.table_renames = table_renames
        # (schema, conn_table_name) -> {conn_column_name: metadata_name}
        self.column_renames = column_renames
        self._table_inverse = {
            (schema, new): old
            for (schema, old), new in table_renames.items()
        }

    def table_target(self, schema: str | None, conn_tname: str) -> str:
        """Return the metadata-side name for a database-side table name."""
        return self.table_renames.get((schema, conn_tname), conn_tname)

    def table_source(self, schema: str | None, meta_tname: str) -> str:
        """Return the database-side name for a metadata-side table name."""
        return self._table_inverse.get((schema, meta_tname), meta_tname)

    def column_renames_for(
        self, schema: str | None, conn_tname: str
    ) -> dict[str, str]:
        return self.column_renames.get((schema, conn_tname), {})

    def translate_column_name(
        self, schema: str | None, conn_tname: str, name: str
    ) -> str:
        return self.column_renames.get((schema, conn_tname), {}).get(
            name, name
        )

    def translate_conn_table(
        self, conn_table: Table, new_tname: str
    ) -> Table:
        """Return a copy of a reflected table in which renamed columns
        (and the table itself, if renamed) use their metadata-side names.

        This copy is used only for constraint/index comparison, so that
        constraints that refer to renamed columns are matched correctly
        against the metadata.

        """
        colmap = self.column_renames.get(
            (conn_table.schema, conn_table.name), {}
        )
        new_metadata = MetaData()
        new_table = Table(
            new_tname,
            new_metadata,
            schema=conn_table.schema,
            comment=conn_table.comment,
        )
        for col in conn_table.c:
            new_table.append_column(
                Column(
                    colmap.get(col.name, col.name),
                    col.type,
                    nullable=col.nullable,
                    server_default=col.server_default,
                    comment=col.comment,
                    primary_key=col.primary_key,
                    autoincrement=col.autoincrement,
                )
            )
        return new_table


def _table_key(key: Any) -> tuple[str | None, str]:
    if isinstance(key, str):
        return (None, key)
    elif (
        isinstance(key, tuple)
        and len(key) == 2
        and (key[0] is None or isinstance(key[0], str))
        and isinstance(key[1], str)
    ):
        return (key[0], key[1])
    raise util.CommandError(
        "rename_map 'tables' keys must be table names or "
        "(schema, table_name) tuples; got %r" % (key,)
    )


def _column_key(key: Any) -> tuple[str | None, str, str]:
    if (
        isinstance(key, tuple)
        and len(key) == 2
        and all(isinstance(elem, str) for elem in key)
    ):
        return (None, key[0], key[1])
    elif (
        isinstance(key, tuple)
        and len(key) == 3
        and (key[0] is None or isinstance(key[0], str))
        and isinstance(key[1], str)
        and isinstance(key[2], str)
    ):
        return (key[0], key[1], key[2])
    raise util.CommandError(
        "rename_map 'columns' keys must be (table_name, column_name) or "
        "(schema, table_name, column_name) tuples; got %r" % (key,)
    )


def _normalize_pairs(param, kind, keyfn):
    if param is None:
        return {}
    if isinstance(param, Mapping):
        items = list(param.items())
    elif isinstance(param, Sequence) and not isinstance(param, str):
        items = []
        for pair in param:
            try:
                key, value = pair
            except (TypeError, ValueError):
                raise util.CommandError(
                    "rename_map %r entries must be (key, value) pairs; "
                    "got %r" % (kind, pair)
                )
            items.append((key, value))
    else:
        raise util.CommandError(
            "rename_map %r must be a mapping or a sequence of "
            "(key, value) pairs" % (kind,)
        )
    result = {}
    for key, value in items:
        norm_key = keyfn(key)
        if not isinstance(value, str):
            raise util.CommandError(
                "rename_map entry %r must map to a string name; got %r"
                % (key, value)
            )
        if norm_key in result:
            raise util.CommandError(
                "rename_map entry %s maps source %r more than once "
                "(one-to-many rename is not supported)"
                % (_format_entry(norm_key, value), norm_key[-1])
            )
        result[norm_key] = value
    return result


def _check_cycles(pairs, kind):
    graph = dict(pairs)
    for start in graph:
        seen = set()
        node = start
        while node in graph:
            if node in seen:
                raise util.CommandError(
                    "rename_map contains a circular %s rename involving "
                    "%r; circular renames cannot be ordered and are not "
                    "supported" % (kind, node[-1])
                )
            seen.add(node)
            node = graph[node]


def _format_name(schema: str | None, name: str) -> str:
    return "%s.%s" % (schema, name) if schema else name


def _format_entry(key: tuple, value: str) -> str:
    names = [str(elem) for elem in key if elem is not None]
    return "%s -> %r" % (".".join(names), value)

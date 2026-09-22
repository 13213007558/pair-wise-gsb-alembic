# mypy: allow-untyped-defs, allow-untyped-calls, allow-incomplete-defs

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import NamedTuple

from sqlalchemy import schema as sa_schema

from .. import util


class _RenameEntry(NamedTuple):
    schema: str | None
    table_name: str
    old_name: str
    new_name: str

    def describe(self) -> str:
        table = (
            "%s.%s" % (self.schema, self.table_name)
            if self.schema
            else self.table_name
        )
        return "%r -> %r on table %r" % (self.old_name, self.new_name, table)


def _norm_schema(schema: str | None, default_schema: str | None) -> str | None:
    if schema is None or schema == default_schema:
        return None
    return schema


class ColumnRenameMap:
    """Explicit, user-supplied column rename mapping used by autogenerate.

    The mapping is keyed by table identity (schema, table name) plus the
    existing column name, and indicates the new name that column should
    be compared against.  No name-similarity guessing is performed; only
    columns named explicitly in the mapping are treated as renames.

    """

    def __init__(self, entries: Sequence[_RenameEntry]) -> None:
        self._entries = list(entries)
        self._consumed: set[int] = set()
        self._table_cache: dict[
            tuple[str | None, str], list[tuple[str, str]]
        ] = {}

    @classmethod
    def from_user_input(cls, renames: Any) -> ColumnRenameMap:
        """Parse the column_renames option into a :class:`ColumnRenameMap`.

        Accepted forms are a mapping of (schema, table_name, old_name)
        (or (table_name, old_name)) tuple keys to new column names, or a
        sequence of (key, new_name) pairs using the same key form.

        """
        if renames is None:
            return cls([])

        if isinstance(renames, Mapping):
            items = list(renames.items())
        elif isinstance(renames, Sequence) and not isinstance(renames, str):
            items = list(renames)
        else:
            raise util.CommandError(
                "column_renames must be a mapping of "
                "(schema, table_name, old_name) -> new_name, or a "
                "sequence of (key, new_name) pairs; got %r" % (renames,)
            )

        entries = []
        for item in items:
            try:
                key, new_name = item
            except (TypeError, ValueError):
                raise util.CommandError(
                    "Invalid column_renames entry %r; expected "
                    "((schema, table_name, old_name), new_name)" % (item,)
                )
            if not isinstance(key, tuple) or len(key) not in (2, 3):
                raise util.CommandError(
                    "Invalid column_renames key %r; expected a "
                    "(schema, table_name, old_name) or "
                    "(table_name, old_name) tuple" % (key,)
                )
            if len(key) == 2:
                schema, table_name, old_name = None, key[0], key[1]
            else:
                schema, table_name, old_name = key
            entries.append(
                _RenameEntry(schema, table_name, old_name, new_name)
            )
        return cls(entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def _entries_for_table(
        self, schema: str | None, tname: str, default_schema: str | None
    ) -> list[tuple[int, _RenameEntry]]:
        schema = _norm_schema(schema, default_schema)
        return [
            (idx, entry)
            for idx, entry in enumerate(self._entries)
            if entry.table_name == tname
            and _norm_schema(entry.schema, default_schema) == schema
        ]

    def renames_for_table(
        self, schema: str | None, tname: str, default_schema: str | None
    ) -> dict[str, str]:
        """Return the validated, execution-ordered old->new rename mapping
        for the given table, marking the entries as consumed.

        Structural errors (one source renamed to several targets, several
        sources renamed to the same target, cyclic renames) are raised
        here, before any migration operations are produced.

        """
        cache_key = (_norm_schema(schema, default_schema), tname)
        if cache_key in self._table_cache:
            return dict(self._table_cache[cache_key])

        matched = self._entries_for_table(schema, tname, default_schema)
        for idx, _ in matched:
            self._consumed.add(idx)

        by_source: dict[str, _RenameEntry] = {}
        by_target: dict[str, _RenameEntry] = {}
        ordered: list[_RenameEntry] = []
        seen_identical: set[_RenameEntry] = set()
        for _, entry in matched:
            if entry in seen_identical:
                continue
            seen_identical.add(entry)
            if entry.old_name in by_source:
                raise util.CommandError(
                    "Column rename mapping is ambiguous: source column "
                    "is renamed to more than one target: %s conflicts "
                    "with %s"
                    % (by_source[entry.old_name].describe(), entry.describe())
                )
            if entry.new_name in by_target:
                raise util.CommandError(
                    "Column rename mapping is ambiguous: more than one "
                    "source is renamed to the same target: %s conflicts "
                    "with %s"
                    % (by_target[entry.new_name].describe(), entry.describe())
                )
            by_source[entry.old_name] = entry
            by_target[entry.new_name] = entry
            ordered.append(entry)

        # order renames so that chained renames (a -> b, b -> c) are
        # emitted in an executable order: the rename that frees up a
        # name (b -> c) comes before the rename that claims it (a -> b).
        result: list[tuple[str, str]] = []
        remaining = list(ordered)
        while remaining:
            pending_sources = {e.old_name for e in remaining}
            progressed = False
            still_remaining = []
            for entry in remaining:
                if entry.new_name in pending_sources:
                    still_remaining.append(entry)
                else:
                    result.append((entry.old_name, entry.new_name))
                    progressed = True
            if not progressed:
                raise util.CommandError(
                    "Column rename mapping contains a cyclic rename: %s"
                    % ", ".join(e.describe() for e in still_remaining)
                )
            remaining = still_remaining

        self._table_cache[cache_key] = result
        return dict(result)

    def validate_all_consumed(self) -> None:
        """Ensure every mapping entry was matched to an existing table.

        Entries that were never consumed refer to tables that are not
        present in both the database and the target metadata, meaning
        the rename source cannot be found.

        """
        missing = [
            entry
            for idx, entry in enumerate(self._entries)
            if idx not in self._consumed
        ]
        if missing:
            raise util.CommandError(
                "Column rename mapping refers to tables that are not "
                "present in both the database and the target metadata; "
                "rename source not found for: %s"
                % ", ".join(entry.describe() for entry in missing)
            )


def renamed_table_copy(
    conn_table: sa_schema.Table, renames: dict[str, str]
) -> sa_schema.Table:
    """Return a lightweight copy of a reflected table in which the
    renamed columns carry their new names.

    This is used so that reflected constraints and indexes that refer
    to renamed columns can be compared against the metadata-side
    objects, which already use the new names.

    """
    metadata = sa_schema.MetaData()
    return sa_schema.Table(
        conn_table.name,
        metadata,
        *[
            sa_schema.Column(renames.get(col.name, col.name), col.type)
            for col in conn_table.c
        ],
        schema=conn_table.schema,
    )


def validate_table_renames(
    renames: dict[str, str],
    schema: str | None,
    tname: str,
    conn_column_names: set[str],
    metadata_column_names: set[str],
) -> dict[str, str]:
    """Validate the rename mapping for one table against the actual
    columns present in the database and in the target metadata,
    returning the renames that still need to be applied.

    This runs before any migration operations for the table are
    produced, and raises for mappings whose source column does not
    exist, whose target name is occupied by an unrelated existing
    column, or whose target column is not present in the target metadata.

    A rename whose source column is gone while its target is already
    present in the database (or is itself renamed away by another
    entry) has already been applied to the database; it is silently
    dropped from the returned mapping so that repeated autogenerate
    runs against a migrated database stay clean.

    A rename whose target is not present in the metadata is skipped
    silently if the source column is still present in the metadata as
    well, meaning the mapping simply does not apply to this metadata;
    otherwise the mapping is contradictory and an error is raised.

    """
    table = "%s.%s" % (schema, tname) if schema else tname
    sources = set(renames)
    active: dict[str, str] = {}
    for old_name, new_name in renames.items():
        entry = "%r -> %r on table %r" % (old_name, new_name, table)
        if old_name not in conn_column_names:
            if new_name in conn_column_names or new_name in sources:
                # already applied to the database
                continue
            raise util.CommandError(
                "Column rename mapping refers to a source column that "
                "does not exist in the database: %s" % entry
            )
        if new_name in conn_column_names and new_name not in sources:
            raise util.CommandError(
                "Column rename mapping target is occupied by an "
                "unrelated existing column: %s" % entry
            )
        if new_name not in metadata_column_names:
            if old_name in metadata_column_names:
                # the metadata still has the source column and does not
                # have the target; the mapping does not apply here
                continue
            raise util.CommandError(
                "Column rename mapping refers to a target column that "
                "is not present in the target metadata: %s" % entry
            )
        active[old_name] = new_name
    return active

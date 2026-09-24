"""Explicit declaration of database views managed by Alembic.

This module provides the :class:`.ManagedView` construct, which allows
database views to be declared alongside the tables in a
:class:`~sqlalchemy.schema.MetaData` collection so that they participate
in autogenerate, operation rendering, and downgrade generation.

.. versionadded:: 1.21.0

Currently, plain views on SQLite are supported.

A managed view is declared with a name, a SQL query definition, an
optional schema, and an optional list of dependencies::

    from alembic.views import ManagedView

    v1 = ManagedView(
        "v1",
        "SELECT id, name FROM t1",
        depends_on=["t1"],
        metadata=target_metadata,
    )
    v2 = ManagedView(
        "v2",
        "SELECT id FROM v1",
        depends_on=[v1],
        metadata=target_metadata,
    )

Views that exist in the database but are not declared as managed are
never dropped or otherwise modified by autogenerate.

"""

from __future__ import annotations

from collections.abc import Sequence
import re
from typing import TYPE_CHECKING
from typing import Union

from sqlalchemy.sql.schema import Table

from . import util
from .util.exc import CommandError

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import quoted_name
    from sqlalchemy.sql.schema import MetaData

_MANAGED_VIEWS_INFO_KEY = "alembic.managed_views"


class ManagedViewError(CommandError):
    """Raised for errors in managed view declarations or operations."""


class ManagedView:
    """Declare a database view that is managed by Alembic.

    :param name: the name of the view.  May be a string or a
     :class:`~sqlalchemy.sql.elements.quoted_name` to control quoting.
    :param definition: the SQL query definition of the view, i.e. the
     ``SELECT`` statement that follows ``CREATE VIEW <name> AS``.
    :param schema: optional schema name the view lives in.
    :param depends_on: optional sequence of dependencies of this view.
     Entries may be other :class:`.ManagedView` objects,
     :class:`~sqlalchemy.schema.Table` objects, or string names, where a
     string may be a plain ``"name"`` or a schema-qualified
     ``"schemaname.name"``.  Dependencies are used to order view
     creation and to determine which views must be dropped and recreated
     when a dependency changes.
    :param metadata: optional :class:`~sqlalchemy.schema.MetaData` to
     register this view with.  Views may also be registered after the
     fact using :func:`.register_view`.

    """

    def __init__(
        self,
        name: str | quoted_name,
        definition: str,
        *,
        schema: str | quoted_name | None = None,
        depends_on: Sequence[Union[str, "ManagedView", "Table"]] = (),
        metadata: MetaData | None = None,
    ) -> None:
        if not isinstance(definition, str) or not definition.strip():
            raise ManagedViewError(
                f"managed view {name!r} requires a non-empty SQL query "
                "definition string"
            )
        self.name = name
        self.schema = schema
        self.definition = definition.strip()
        self.depends_on = tuple(
            self._dependency_name(dep) for dep in depends_on
        )
        if metadata is not None:
            register_view(metadata, self)

    @staticmethod
    def _dependency_name(dep: str | ManagedView | Table) -> str:
        if isinstance(dep, ManagedView):
            return dep.dotted_name
        elif isinstance(dep, str):
            if not dep.strip():
                raise ManagedViewError(
                    "view dependencies may not be blank strings"
                )
            return dep
        elif isinstance(dep, Table):
            schema = dep.schema
            return f"{schema}.{dep.name}" if schema else str(dep.name)
        else:
            raise ManagedViewError(
                "view dependencies must be strings, ManagedView objects, "
                f"or Table objects; got {dep!r}"
            )

    @property
    def dotted_name(self) -> str:
        """The schema-qualified name of this view as a string."""
        if self.schema:
            return f"{self.schema}.{self.name}"
        return str(self.name)

    @property
    def key(self) -> tuple[str | None, str]:
        """The ``(schema, name)`` identity of this view."""
        return (
            str(self.schema) if self.schema is not None else None,
            str(self.name),
        )

    def __repr__(self) -> str:
        return f"ManagedView({self.dotted_name!r})"


def register_view(metadata: MetaData, view: ManagedView) -> ManagedView:
    """Register a :class:`.ManagedView` with a
    :class:`~sqlalchemy.schema.MetaData` collection.

    """
    views: list[ManagedView] = metadata.info.setdefault(
        _MANAGED_VIEWS_INFO_KEY, []
    )
    keys = {v.key for v in views}
    if view.key in keys:
        raise ManagedViewError(
            f"managed view {view.dotted_name!r} is already registered "
            "with this MetaData"
        )
    views.append(view)
    return view


def managed_views(
    metadata: MetaData | Sequence[MetaData] | None,
) -> list[ManagedView]:
    """Return all :class:`.ManagedView` objects registered with the
    given :class:`~sqlalchemy.schema.MetaData` object or sequence of
    objects.

    """
    if metadata is None:
        return []
    result: list[ManagedView] = []
    for m in util.to_list(metadata):
        result.extend(m.info.get(_MANAGED_VIEWS_INFO_KEY, ()))
    return result


def split_dependency_name(name: str) -> tuple[str | None, str]:
    """Split a ``"schema.name"`` or ``"name"`` dependency string
    into a ``(schema, name)`` tuple."""
    if "." in name:
        schema, _, objname = name.rpartition(".")
        return schema, objname
    return None, name


def normalize_view_definition(sql: str) -> str:
    """Normalize a view query definition for comparison.

    The rule is deliberately conservative: runs of whitespace that occur
    *outside* of string constants and quoted identifiers are collapsed to
    a single space, leading and trailing whitespace is removed, and
    trailing semicolons are discarded.  Everything else, including the
    contents of string constants and the case of keywords, is compared
    exactly.  This means superficial formatting differences do not
    produce spurious autogenerate operations, while any substantive
    difference does.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    pending_space = False
    quote: str | None = None
    end_quote: str | None = None
    while i < n:
        ch = sql[i]
        if quote is None:
            if ch.isspace():
                pending_space = True
                i += 1
                continue
            if pending_space and out:
                out.append(" ")
            pending_space = False
            if ch == "'":
                quote = end_quote = "'"
            elif ch == '"':
                quote = end_quote = '"'
            elif ch == "`":
                quote = end_quote = "`"
            elif ch == "[":
                quote = "["
                end_quote = "]"
            out.append(ch)
            i += 1
        else:
            out.append(ch)
            if ch == end_quote:
                if quote != "[" and i + 1 < n and sql[i + 1] == end_quote:
                    # doubled quote escape inside a quoted section
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = end_quote = None
            i += 1
    normalized = "".join(out).strip()
    while normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    return normalized


_CREATE_VIEW_RE = re.compile(
    r"^\s*CREATE\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?", re.IGNORECASE
)


def extract_view_query(reflected_sql: str) -> str:
    """Given a reflected ``CREATE VIEW`` statement, return just the
    query portion that follows ``CREATE VIEW <name> AS``.

    If the statement does not look like a ``CREATE VIEW`` statement,
    it is returned unchanged; comparison will then conservatively treat
    it as different from any declared query.
    """
    match = _CREATE_VIEW_RE.match(reflected_sql)
    if match is None:
        return reflected_sql
    i = match.end()
    n = len(reflected_sql)
    # skip over the (possibly schema-qualified, possibly quoted) view name
    while i < n:
        ch = reflected_sql[i]
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            while i < n:
                if reflected_sql[i] == quote:
                    if i + 1 < n and reflected_sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif ch == "[":
            end = reflected_sql.find("]", i + 1)
            i = n if end == -1 else end + 1
        elif ch.isspace():
            i += 1
            break
        else:
            i += 1
    as_match = re.match(r"\s*AS\s+", reflected_sql[i:], re.IGNORECASE)
    if as_match is None:
        return reflected_sql
    return reflected_sql[i + as_match.end() :]

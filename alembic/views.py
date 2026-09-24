# mypy: allow-untyped-defs, allow-incomplete-defs, allow-untyped-calls

from __future__ import annotations

import re
from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import quoted_name
    from sqlalchemy.sql.schema import MetaData

MANAGED_VIEWS_INFO_KEY = "alembic.managed_views"


class View:
    """Declare a database view that is managed by Alembic.

    A :class:`.View` instance names a database view, the SQL query that
    defines it, and the objects it depends upon.  Instances are associated
    with a :class:`~sqlalchemy.schema.MetaData` collection, either by
    passing the ``metadata`` parameter or by using
    :func:`.register_views`::

        from alembic.views import View

        View(
            "v1",
            "SELECT id, name FROM t1",
            depends_on=["t1"],
            metadata=my_metadata,
        )
        View(
            "v2",
            "SELECT id FROM v1",
            depends_on=["v1"],
            metadata=my_metadata,
        )

    When the :class:`~sqlalchemy.schema.MetaData` collection is used as
    the ``target_metadata`` of an autogenerate operation, the declared
    views are compared against the views present in the database, and
    :meth:`.Operations.create_view` / :meth:`.Operations.drop_view`
    operations are produced as needed.  Views are always dropped before
    the tables they depend on are altered, and are recreated afterwards
    in dependency order.

    Views that exist in the database but are not declared are left
    untouched.

    """

    def __init__(
        self,
        name: str | quoted_name,
        definition: str,
        *,
        schema: str | quoted_name | None = None,
        depends_on: Any = (),
        metadata: MetaData | None = None,
    ) -> None:
        self.name = name
        self.definition = definition
        self.schema = schema
        if not isinstance(depends_on, (list, tuple, set, frozenset)):
            depends_on = (depends_on,)
        self.depends_on = tuple(depends_on)
        if metadata is not None:
            register_views(metadata, self)

    @property
    def key(self) -> tuple[str | None, str]:
        return (
            str(self.schema) if self.schema is not None else None,
            str(self.name),
        )

    def __repr__(self) -> str:
        return "View(%r, %r, schema=%r)" % (
            str(self.name),
            self.definition,
            str(self.schema) if self.schema is not None else None,
        )


def register_views(metadata: MetaData, *views: View) -> None:
    """Register one or more :class:`.View` objects against a
    :class:`~sqlalchemy.schema.MetaData` collection, so that they
    participate in autogenerate comparisons.

    """
    declared = metadata.info.setdefault(MANAGED_VIEWS_INFO_KEY, [])
    existing = {view.key for view in declared}
    for view in views:
        if view.key in existing:
            raise ValueError(
                "View %r is already registered against this MetaData"
                % (view.key,)
            )
        existing.add(view.key)
        declared.append(view)


_create_view_prefix_re = re.compile(
    r"^\s*CREATE\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[\w]+)"
    r"(?:\s*\.\s*(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[\w]+))*"
    r"\s+AS\s+",
    re.IGNORECASE,
)


def _strip_create_view_prefix(definition: str) -> str:
    """Remove a leading ``CREATE VIEW ... AS`` clause, if present."""
    return _create_view_prefix_re.sub("", definition, count=1)


def _collapse_whitespace_outside_literals(definition: str) -> str:
    """Collapse runs of whitespace to single spaces, leaving any quoted
    string literals and quoted identifiers exactly as written.

    """
    out: list[str] = []
    idx = 0
    size = len(definition)
    pending_space = False
    while idx < size:
        char = definition[idx]
        if char in ("'", '"', "`"):
            quote = char
            end = idx + 1
            while end < size:
                if definition[end] == quote:
                    if end + 1 < size and definition[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(definition[idx:end])
            idx = end
        elif char == "[":
            end = definition.find("]", idx + 1)
            end = size if end == -1 else end + 1
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(definition[idx:end])
            idx = end
        elif char.isspace():
            pending_space = True
            idx += 1
        else:
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(char)
            idx += 1
    return "".join(out)


def normalize_view_definition(definition: str) -> str:
    """Return a normalized form of a view definition suitable for
    comparison.

    The rule is deliberately conservative: a leading
    ``CREATE VIEW ... AS`` clause and trailing semicolons are removed,
    and runs of whitespace *outside* of quoted string literals and quoted
    identifiers are collapsed to single spaces.  The contents of string
    literals are never modified.  The normalized forms are then compared
    for exact equality.

    """
    definition = _strip_create_view_prefix(definition)
    definition = definition.strip()
    while definition.endswith(";"):
        definition = definition[:-1].strip()
    return _collapse_whitespace_outside_literals(definition)

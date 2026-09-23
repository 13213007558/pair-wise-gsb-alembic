# mypy: allow-untyped-defs, allow-incomplete-defs, allow-untyped-calls
# mypy: no-warn-return-any, allow-any-generics

from __future__ import annotations

import re
from typing import Any
from typing import NamedTuple
from typing import TYPE_CHECKING

from sqlalchemy import cast
from sqlalchemy import Computed
from sqlalchemy import JSON
from sqlalchemy import schema
from sqlalchemy import sql

from .base import alter_table
from .base import ColumnName
from .base import format_column_name
from .base import format_table_name
from .base import RenameTable
from .impl import DefaultImpl
from .. import util
from ..util.sqla_compat import compiles

if TYPE_CHECKING:
    from sqlalchemy.engine.reflection import Inspector
    from sqlalchemy.sql.compiler import DDLCompiler
    from sqlalchemy.sql.elements import Cast
    from sqlalchemy.sql.elements import ClauseElement
    from sqlalchemy.sql.schema import Column
    from sqlalchemy.sql.schema import Constraint
    from sqlalchemy.sql.schema import Table
    from sqlalchemy.sql.type_api import TypeEngine

    from ..operations.batch import BatchOperationsImpl


class SQLiteImpl(DefaultImpl):
    __dialect__ = "sqlite"

    transactional_ddl = False
    """SQLite supports transactional DDL, but pysqlite does not:
    see: http://bugs.python.org/issue10740
    """

    def requires_recreate_in_batch(
        self, batch_op: BatchOperationsImpl
    ) -> bool:
        """Return True if the given :class:`.BatchOperationsImpl`
        would need the table to be recreated and copied in order to
        proceed.

        Normally, only returns True on SQLite when operations other
        than add_column are present.

        """
        for op in batch_op.batch:
            if op[0] == "add_column":
                col = op[1][1]
                if isinstance(
                    col.server_default, schema.DefaultClause
                ) and isinstance(col.server_default.arg, sql.ClauseElement):
                    return True
                elif (
                    isinstance(col.server_default, Computed)
                    and col.server_default.persisted
                ):
                    return True
            elif op[0] not in ("create_index", "drop_index"):
                return True
        else:
            return False

    def add_constraint(self, const: Constraint, **kw: Any):
        # attempt to distinguish between an
        # auto-gen constraint and an explicit one
        if const._create_rule is None:
            raise NotImplementedError(
                "No support for ALTER of constraints in SQLite dialect. "
                "Please refer to the batch mode feature which allows for "
                "SQLite migrations using a copy-and-move strategy."
            )
        elif const._create_rule(self):
            util.warn(
                "Skipping unsupported ALTER for "
                "creation of implicit constraint. "
                "Please refer to the batch mode feature which allows for "
                "SQLite migrations using a copy-and-move strategy."
            )

    def drop_constraint(self, const: Constraint, **kw: Any):
        if const._create_rule is None:
            raise NotImplementedError(
                "No support for ALTER of constraints in SQLite dialect. "
                "Please refer to the batch mode feature which allows for "
                "SQLite migrations using a copy-and-move strategy."
            )

    def compare_server_default(
        self,
        inspector_column: Column[Any],
        metadata_column: Column[Any],
        rendered_metadata_default: str | None,
        rendered_inspector_default: str | None,
    ) -> bool:
        if rendered_metadata_default is not None:
            rendered_metadata_default = re.sub(
                r"^\((.+)\)$", r"\1", rendered_metadata_default
            )

            rendered_metadata_default = re.sub(
                r"^\"?'(.+)'\"?$", r"\1", rendered_metadata_default
            )

        if rendered_inspector_default is not None:
            rendered_inspector_default = re.sub(
                r"^\((.+)\)$", r"\1", rendered_inspector_default
            )

            rendered_inspector_default = re.sub(
                r"^\"?'(.+)'\"?$", r"\1", rendered_inspector_default
            )

        return rendered_inspector_default != rendered_metadata_default

    def _guess_if_default_is_unparenthesized_sql_expr(
        self, expr: str | None
    ) -> bool:
        """Determine if a server default is a SQL expression or a constant.

        There are too many assertions that expect server defaults to round-trip
        identically without parenthesis added so we will add parens only in
        very specific cases.

        """
        if not expr:
            return False
        elif re.match(r"^[0-9\.]$", expr):
            return False
        elif re.match(r"^'.+'$", expr):
            return False
        elif re.match(r"^\(.+\)$", expr):
            return False
        else:
            return True

    def autogen_column_reflect(
        self,
        inspector: Inspector,
        table: Table,
        column_info: dict[str, Any],
    ) -> None:
        # SQLite expression defaults require parenthesis when sent
        # as DDL
        if self._guess_if_default_is_unparenthesized_sql_expr(
            column_info.get("default", None)
        ):
            column_info["default"] = "(%s)" % (column_info["default"],)

    def render_ddl_sql_expr(
        self, expr: ClauseElement, is_server_default: bool = False, **kw
    ) -> str:
        # SQLite expression defaults require parenthesis when sent
        # as DDL
        str_expr = super().render_ddl_sql_expr(
            expr, is_server_default=is_server_default, **kw
        )

        if (
            is_server_default
            and self._guess_if_default_is_unparenthesized_sql_expr(str_expr)
        ):
            str_expr = "(%s)" % (str_expr,)
        return str_expr

    def cast_for_batch_migrate(
        self,
        existing: Column[Any],
        existing_transfer: dict[str, TypeEngine | Cast],
        new_type: TypeEngine,
    ) -> None:
        if (
            existing.type._type_affinity is not new_type._type_affinity
            and not isinstance(new_type, JSON)
        ):
            existing_transfer["expr"] = cast(
                existing_transfer["expr"], new_type
            )

    def correct_for_autogen_constraints(
        self,
        conn_unique_constraints,
        conn_indexes,
        metadata_unique_constraints,
        metadata_indexes,
    ):
        self._skip_functional_indexes(metadata_indexes, conn_indexes)


@compiles(RenameTable, "sqlite")
def visit_rename_table(
    element: RenameTable, compiler: DDLCompiler, **kw
) -> str:
    return "%s RENAME TO %s" % (
        alter_table(compiler, element.table_name, element.schema),
        format_table_name(compiler, element.new_table_name, None),
    )


@compiles(ColumnName, "sqlite")
def visit_column_name(element: ColumnName, compiler: DDLCompiler, **kw) -> str:
    return "%s RENAME COLUMN %s TO %s" % (
        alter_table(compiler, element.table_name, element.schema),
        format_column_name(compiler, element.column_name),
        format_column_name(compiler, element.newname),
    )


# @compiles(AddColumn, 'sqlite')
# def visit_add_column(element, compiler, **kw):
#    return "%s %s" % (
#        alter_table(compiler, element.table_name, element.schema),
#        add_column(compiler, element.column, **kw)
#    )


# def add_column(compiler, column, **kw):
#    text = "ADD COLUMN %s" % compiler.get_column_specification(column, **kw)
# need to modify SQLAlchemy so that the CHECK associated with a Boolean
# or Enum gets placed as part of the column constraints, not the Table
# see ticket 98
#    for const in column.constraints:
#        text += compiler.process(AddConstraint(const))
#    return text


class SQLiteTriggerParseError(Exception):
    """Raised when a SQLite CREATE TRIGGER statement cannot be analyzed."""


class SQLiteTrigger(NamedTuple):
    """Structure of a parsed SQLite CREATE TRIGGER statement."""

    name: str
    schema: str | None
    table_name: str
    table_schema: str | None
    is_temp: bool
    column_refs: frozenset[str]
    """names of columns of the trigger's own table that the trigger
    refers to, via the UPDATE OF column list as well as OLD./NEW./
    <table>. qualified references."""


class _TriggerToken(NamedTuple):
    kind: str  # "ident", "qident", "string", "punct", "other"
    value: str


_TRIGGER_NAME_KINDS = ("ident", "qident", "string")

_DOT = _TriggerToken("punct", ".")
_COMMA = _TriggerToken("punct", ",")
_SEMICOLON = _TriggerToken("punct", ";")


def _consume_quoted_token(sql: str, start: int, quote: str) -> tuple[str, int]:
    # sql[start] == quote; handles doubled-quote escapes
    i = start + 1
    chars = []
    while True:
        j = sql.find(quote, i)
        if j == -1:
            raise SQLiteTriggerParseError("unterminated quoted token")
        chars.append(sql[i:j])
        if j + 1 < len(sql) and sql[j + 1] == quote:
            chars.append(quote)
            i = j + 2
        else:
            return "".join(chars), j + 1


def _tokenize_trigger_sql(sql: str) -> list[_TriggerToken]:
    tokens: list[_TriggerToken] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i + 2)
            i = n if j == -1 else j + 1
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j == -1:
                raise SQLiteTriggerParseError("unterminated comment")
            i = j + 2
        elif ch == "'":
            value, i = _consume_quoted_token(sql, i, "'")
            tokens.append(_TriggerToken("string", value))
        elif ch == '"' or ch == "`":
            value, i = _consume_quoted_token(sql, i, ch)
            tokens.append(_TriggerToken("qident", value))
        elif ch == "[":
            j = sql.find("]", i + 1)
            if j == -1:
                raise SQLiteTriggerParseError(
                    "unterminated [..] quoted identifier"
                )
            tokens.append(_TriggerToken("qident", sql[i + 1 : j]))
            i = j + 1
        elif ch.isalpha() or ch == "_" or ord(ch) > 127:
            j = i + 1
            while j < n and (
                sql[j].isalnum() or sql[j] in "_$" or ord(sql[j]) > 127
            ):
                j += 1
            tokens.append(_TriggerToken("ident", sql[i:j]))
            i = j
        elif ch.isdigit():
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "."):
                j += 1
            tokens.append(_TriggerToken("other", sql[i:j]))
            i = j
        else:
            tokens.append(_TriggerToken("punct", ch))
            i += 1
    return tokens


def _is_kw(token: _TriggerToken, *keywords: str) -> bool:
    return token.kind == "ident" and token.value.upper() in keywords


def _parse_trigger_qualified_name(
    tokens: list[_TriggerToken], pos: int
) -> tuple[str | None, str, int]:
    if pos >= len(tokens) or tokens[pos].kind not in _TRIGGER_NAME_KINDS:
        raise SQLiteTriggerParseError("expected a name")
    first = tokens[pos].value
    pos += 1
    if pos < len(tokens) and tokens[pos] == _DOT:
        if (
            pos + 1 >= len(tokens)
            or tokens[pos + 1].kind not in _TRIGGER_NAME_KINDS
        ):
            raise SQLiteTriggerParseError("expected a name after '.'")
        return first, tokens[pos + 1].value, pos + 2
    return None, first, pos


def parse_create_trigger(sql: str) -> SQLiteTrigger:
    """Parse a SQLite CREATE TRIGGER statement.

    The statement is tokenized and its header is validated; column
    dependencies of the trigger on its own table are collected from the
    UPDATE OF column list as well as OLD., NEW. and <table>. qualified
    references found anywhere in the WHEN clause or the trigger body,
    including multi-statement bodies.  Quoted names (double quotes,
    backticks, square brackets, and string-literal style single quotes)
    are recognized.

    :raises SQLiteTriggerParseError: if the statement cannot be
     analyzed.  Callers must treat this as "unsafe" and must not fall
     back to textual manipulation of the statement.

    """
    tokens = _tokenize_trigger_sql(sql)
    if not tokens or not _is_kw(tokens[0], "CREATE"):
        raise SQLiteTriggerParseError("statement does not start with CREATE")
    pos = 1
    is_temp = False
    if pos < len(tokens) and _is_kw(tokens[pos], "TEMP", "TEMPORARY"):
        is_temp = True
        pos += 1
    if pos >= len(tokens) or not _is_kw(tokens[pos], "TRIGGER"):
        raise SQLiteTriggerParseError("statement is not a CREATE TRIGGER")
    pos += 1
    if (
        pos + 2 < len(tokens)
        and _is_kw(tokens[pos], "IF")
        and _is_kw(tokens[pos + 1], "NOT")
        and _is_kw(tokens[pos + 2], "EXISTS")
    ):
        pos += 3
    schema, name, pos = _parse_trigger_qualified_name(tokens, pos)

    if pos < len(tokens) and _is_kw(tokens[pos], "BEFORE", "AFTER"):
        pos += 1
    elif pos < len(tokens) and _is_kw(tokens[pos], "INSTEAD"):
        if pos + 1 >= len(tokens) or not _is_kw(tokens[pos + 1], "OF"):
            raise SQLiteTriggerParseError("expected OF after INSTEAD")
        pos += 2

    column_refs: set[str] = set()
    if pos < len(tokens) and _is_kw(tokens[pos], "INSERT", "DELETE"):
        pos += 1
    elif pos < len(tokens) and _is_kw(tokens[pos], "UPDATE"):
        pos += 1
        if pos < len(tokens) and _is_kw(tokens[pos], "OF"):
            pos += 1
            while True:
                if (
                    pos >= len(tokens)
                    or tokens[pos].kind not in _TRIGGER_NAME_KINDS
                ):
                    raise SQLiteTriggerParseError(
                        "expected a column name in the UPDATE OF clause"
                    )
                column_refs.add(tokens[pos].value)
                pos += 1
                if pos < len(tokens) and tokens[pos] == _COMMA:
                    pos += 1
                else:
                    break
    else:
        raise SQLiteTriggerParseError("expected INSERT, UPDATE, or DELETE")

    if pos >= len(tokens) or not _is_kw(tokens[pos], "ON"):
        raise SQLiteTriggerParseError("expected ON")
    pos += 1
    table_schema, table_name, pos = _parse_trigger_qualified_name(tokens, pos)

    begin_pos = None
    for j in range(pos, len(tokens)):
        if _is_kw(tokens[j], "BEGIN"):
            begin_pos = j
            break
    if begin_pos is None:
        raise SQLiteTriggerParseError("trigger has no BEGIN..END body")

    case_depth = 0
    end_pos = None
    for j in range(begin_pos + 1, len(tokens)):
        if _is_kw(tokens[j], "CASE"):
            case_depth += 1
        elif _is_kw(tokens[j], "END"):
            if case_depth:
                case_depth -= 1
            else:
                end_pos = j
                break
    if end_pos is None:
        raise SQLiteTriggerParseError("trigger body is not terminated by END")
    rest = tokens[end_pos + 1 :]
    if rest and rest != [_SEMICOLON]:
        raise SQLiteTriggerParseError(
            "unexpected tokens after the trigger body"
        )

    qualifiers = {"OLD", "NEW", table_name.upper()}
    for j, token in enumerate(tokens):
        if token.kind not in ("ident", "qident"):
            continue
        if token.value.upper() not in qualifiers:
            continue
        if j > 0 and tokens[j - 1] == _DOT:
            # the token is itself qualified, e.g. main."t"
            continue
        if (
            j + 2 < len(tokens)
            and tokens[j + 1] == _DOT
            and tokens[j + 2].kind in _TRIGGER_NAME_KINDS
        ):
            column_refs.add(tokens[j + 2].value)

    return SQLiteTrigger(
        name=name,
        schema=schema,
        table_name=table_name,
        table_schema=table_schema,
        is_temp=is_temp,
        column_refs=frozenset(column_refs),
    )

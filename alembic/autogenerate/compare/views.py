# mypy: allow-untyped-calls

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.schema import Table

from ... import util
from ...operations import ops
from ...util import DispatchPriority
from ...util import PriorityDispatchResult
from ...views import _strip_create_view_prefix
from ...views import MANAGED_VIEWS_INFO_KEY
from ...views import normalize_view_definition
from ...views import View

if TYPE_CHECKING:
    from sqlalchemy.engine.reflection import Inspector

    from ...autogenerate.api import AutogenContext
    from ...operations.ops import UpgradeOps
    from ...runtime.plugins import Plugin

log = logging.getLogger(__name__)


def _autogen_for_views(
    autogen_context: AutogenContext,
    upgrade_ops: UpgradeOps,
    schemas: set[str | None],
) -> PriorityDispatchResult:
    declared = _collect_managed_views(autogen_context)
    if not declared:
        return PriorityDispatchResult.CONTINUE

    inspector = autogen_context.inspector
    reflected_views = _reflect_views(autogen_context, inspector, schemas)

    view_deps, table_deps = _resolve_dependencies(
        autogen_context, inspector, schemas, declared, reflected_views
    )
    ordered_keys = _topological_order(declared, view_deps)

    touched_tables, dropped_tables = _affected_tables(upgrade_ops)
    _check_dropped_tables(declared, view_deps, table_deps, dropped_tables)

    rebuild = set()
    for key in ordered_keys:
        if key not in reflected_views:
            continue
        if normalize_view_definition(
            declared[key].definition
        ) != normalize_view_definition(reflected_views[key]):
            rebuild.add(key)
            log.info("Detected changed definition for view %r", key[1])

    # rebuild every managed view that depends on a changed table or on
    # another view that is itself being rebuilt, transitively
    view_dependents: dict[tuple[str | None, str], set] = {}
    table_dependents: dict[tuple[str | None, str], set] = {}
    for key in ordered_keys:
        for dep in view_deps[key]:
            view_dependents.setdefault(dep, set()).add(key)
        for dep in table_deps[key]:
            table_dependents.setdefault(dep, set()).add(key)

    queue = []
    for key in rebuild:
        queue.extend(view_dependents.get(key, ()))
    for tkey in touched_tables:
        queue.extend(table_dependents.get(tkey, ()))
    seen = set(rebuild)
    while queue:
        key = queue.pop()
        if key in seen:
            continue
        seen.add(key)
        if key in reflected_views:
            rebuild.add(key)
            log.info(
                "View %r depends on changed objects and will be rebuilt",
                key[1],
            )
        queue.extend(view_dependents.get(key, ()))

    creates = [
        key
        for key in ordered_keys
        if key in rebuild or key not in reflected_views
    ]

    _validate_new_definitions(
        autogen_context, declared, creates, view_deps, table_deps,
        touched_tables,
    )

    # drop dependent views before any table operation, in reverse
    # dependency order; recreate them afterwards in dependency order
    drop_ops = []
    for key in reversed(ordered_keys):
        if key in rebuild:
            view = declared[key]
            drop_ops.append(
                ops.DropViewOp(
                    view.name,
                    schema=view.schema,
                    _reverse=ops.CreateViewOp(
                        view.name,
                        _strip_create_view_prefix(
                            reflected_views[key]
                        ).strip(),
                        schema=view.schema,
                    ),
                )
            )
            log.info("Detected drop of view %r", key[1])

    create_ops = []
    for key in creates:
        view = declared[key]
        create_ops.append(
            ops.CreateViewOp(view.name, view.definition, schema=view.schema)
        )
        log.info("Detected create of view %r", key[1])

    if drop_ops:
        upgrade_ops.ops[0:0] = drop_ops
    upgrade_ops.ops.extend(create_ops)

    return PriorityDispatchResult.CONTINUE


def _collect_managed_views(
    autogen_context: AutogenContext,
) -> dict[tuple[str | None, str], View]:
    declared: dict[tuple[str | None, str], View] = {}
    for metadata in util.to_list(autogen_context.metadata, []):
        for view in metadata.info.get(MANAGED_VIEWS_INFO_KEY, ()):
            key = view.key
            if key in declared:
                raise util.CommandError(
                    "View %r is declared as a managed view more than once"
                    % (key,)
                )
            if not autogen_context.run_name_filters(
                key[1], "view", {"schema_name": key[0]}
            ):
                continue
            declared[key] = view
    return declared


def _reflect_views(
    autogen_context: AutogenContext,
    inspector: Inspector,
    schemas: set[str | None],
) -> dict[tuple[str | None, str], str]:
    reflected: dict[tuple[str | None, str], str] = {}
    for schema_name in schemas:
        for vname in inspector.get_view_names(schema=schema_name):
            if autogen_context.run_name_filters(
                vname, "view", {"schema_name": schema_name}
            ):
                reflected[(schema_name, vname)] = (
                    inspector.get_view_definition(vname, schema=schema_name)
                )
    return reflected


def _resolve_dependencies(
    autogen_context: AutogenContext,
    inspector: Inspector,
    schemas: set[str | None],
    declared: dict[tuple[str | None, str], View],
    reflected_views: dict[tuple[str | None, str], str],
):
    metadata_tables = set()
    for metadata in util.to_list(autogen_context.metadata, []):
        for table in metadata.tables.values():
            metadata_tables.add(
                (
                    str(table.schema) if table.schema is not None else None,
                    str(table.name),
                )
            )

    reflected_tables: set[tuple[str | None, str]] | None = None

    def _reflected_table_names():
        nonlocal reflected_tables
        if reflected_tables is None:
            reflected_tables = set()
            for schema_name in schemas:
                for tname in inspector.get_table_names(schema=schema_name):
                    reflected_tables.add((schema_name, tname))
        return reflected_tables

    view_deps: dict[tuple[str | None, str], set] = {}
    table_deps: dict[tuple[str | None, str], set] = {}

    for key, view in declared.items():
        view_deps[key] = set()
        table_deps[key] = set()
        for dep in view.depends_on:
            kind, dep_key = _resolve_dependency(
                view,
                dep,
                declared,
                metadata_tables,
                reflected_views,
                _reflected_table_names,
            )
            if kind == "view":
                view_deps[key].add(dep_key)
            elif kind == "table":
                table_deps[key].add(dep_key)
            # "external" objects are not managed; they satisfy the
            # reference but do not participate in rebuilds
    return view_deps, table_deps


def _resolve_dependency(
    view: View,
    dep,
    declared,
    metadata_tables,
    reflected_views,
    reflected_table_names,
):
    if isinstance(dep, View):
        key = dep.key
        if key not in declared:
            raise util.CommandError(
                "Managed view %r depends on view %r, which is not "
                "declared as a managed view" % (view.name, dep.name)
            )
        return "view", key
    elif isinstance(dep, Table):
        return "table", (
            str(dep.schema) if dep.schema is not None else None,
            str(dep.name),
        )

    name = str(dep)
    schema = None
    if "." in name:
        schema, name = name.split(".", 1)
    key = (schema, name)

    if key in declared:
        return "view", key
    if schema is None:
        matches = [k for k in declared if k[1] == name]
        if len(matches) == 1:
            return "view", matches[0]
        elif len(matches) > 1:
            raise util.CommandError(
                "Dependency %r of managed view %r is ambiguous; it "
                "matches managed views in multiple schemas"
                % (dep, view.name)
            )
    if key in metadata_tables:
        return "table", key
    if key in reflected_views:
        return "external", key
    if key in reflected_table_names():
        return "table", key
    raise util.CommandError(
        "Cannot resolve dependency %r of managed view %r: no such table "
        "or view is declared in the target metadata or present in the "
        "database" % (dep, view.name)
    )


def _topological_order(
    declared: dict[tuple[str | None, str], View],
    view_deps: dict[tuple[str | None, str], set],
) -> list[tuple[str | None, str]]:
    remaining = {key: set(deps) for key, deps in view_deps.items()}
    ordered = []
    while remaining:
        ready = [key for key in remaining if not remaining[key]]
        if not ready:
            raise util.CommandError(
                "Dependency cycle detected among managed views: %s"
                % ", ".join(
                    sorted(repr(key[1]) for key in remaining)
                ),
            )
        for key in ready:
            ordered.append(key)
            del remaining[key]
            for deps in remaining.values():
                deps.discard(key)
    return ordered


def _affected_tables(upgrade_ops: UpgradeOps):
    created: set[tuple[str | None, str]] = set()
    modified: set[tuple[str | None, str]] = set()
    dropped: set[tuple[str | None, str]] = set()

    def _key(schema, name):
        return (str(schema) if schema is not None else None, str(name))

    for op in upgrade_ops.ops:
        if isinstance(op, ops.CreateTableOp):
            created.add(_key(op.schema, op.table_name))
        elif isinstance(op, ops.DropTableOp):
            dropped.add(_key(op.schema, op.table_name))
        elif isinstance(op, ops.RenameTableOp):
            modified.add(_key(op.schema, op.table_name))
            modified.add(_key(op.schema, op.new_table_name))
        elif isinstance(op, ops.ModifyTableOps):
            # only column-level changes can affect the views that
            # reference the table; index, constraint and comment
            # changes do not
            if any(
                isinstance(
                    sub_op,
                    (ops.AddColumnOp, ops.DropColumnOp, ops.AlterColumnOp),
                )
                for sub_op in op.ops
            ):
                modified.add(_key(op.schema, op.table_name))

    touched = created | modified | dropped
    dropped_only = dropped - created
    return touched, dropped_only


def _transitive_table_deps(
    key, view_deps, table_deps, visited=None
):
    if visited is None:
        visited = set()
    result = set(table_deps[key])
    for dep in view_deps[key]:
        if dep not in visited:
            visited.add(dep)
            result |= _transitive_table_deps(
                dep, view_deps, table_deps, visited
            )
    return result


def _check_dropped_tables(
    declared, view_deps, table_deps, dropped_tables
) -> None:
    if not dropped_tables:
        return
    for key, view in declared.items():
        bad = _transitive_table_deps(key, view_deps, table_deps).intersection(
            dropped_tables
        )
        if bad:
            names = ", ".join(
                sorted("%s.%s" % (s, n) if s else n for s, n in bad)
            )
            raise util.CommandError(
                "Managed view %r depends on table(s) %s, which are being "
                "dropped; update the view definition or remove it from "
                "the managed views" % (view.name, names)
            )


def _validate_new_definitions(
    autogen_context: AutogenContext,
    declared,
    creates,
    view_deps,
    table_deps,
    touched_tables,
) -> None:
    connection = autogen_context.connection
    if connection is None:
        return
    create_set = set(creates)
    for key in creates:
        # a definition can only be checked against the current database
        # if none of the objects it depends on are themselves being
        # created or altered by this migration
        stack = [key]
        visited = set()
        checkable = True
        while stack and checkable:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            if current != key and current in create_set:
                checkable = False
                break
            if table_deps[current].intersection(touched_tables):
                checkable = False
                break
            stack.extend(view_deps[current])
        if not checkable:
            continue
        view = declared[key]
        select = _strip_create_view_prefix(view.definition).strip()
        select = select.rstrip(";").strip()
        try:
            connection.execute(text("EXPLAIN " + select))
        except SQLAlchemyError as err:
            raise util.CommandError(
                "The definition of managed view %r is not valid against "
                "the current database: %s" % (view.name, err)
            ) from err


def setup(plugin: Plugin) -> None:
    plugin.add_autogenerate_comparator(
        _autogen_for_views,
        "schema",
        "views",
        priority=DispatchPriority.LAST,
    )

# mypy: allow-untyped-calls

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...operations import ops
from ...util import DispatchPriority
from ...util import PriorityDispatchResult
from ...views import extract_view_query
from ...views import managed_views
from ...views import ManagedView
from ...views import ManagedViewError
from ...views import normalize_view_definition
from ...views import split_dependency_name

if TYPE_CHECKING:
    from sqlalchemy.engine.reflection import Inspector

    from ...autogenerate.api import AutogenContext
    from ...operations.ops import UpgradeOps
    from ...runtime.plugins import Plugin


log = logging.getLogger(__name__)

_ViewKey = tuple[str | None, str]


def _autogen_for_views(
    autogen_context: AutogenContext,
    upgrade_ops: UpgradeOps,
    schemas: set[str | None],
) -> PriorityDispatchResult:
    declared = _collect_declared_views(autogen_context)
    if not declared:
        # no managed views; views present in the database are left
        # completely untouched
        return PriorityDispatchResult.CONTINUE

    inspector = autogen_context.inspector

    view_names_by_schema: dict[str | None, set[str]] = {}
    reflected: dict[_ViewKey, str] = {}
    for key in declared:
        schema, name = key
        if name in _view_names(inspector, view_names_by_schema, schema):
            reflected[key] = inspector.get_view_definition(
                name, schema=schema
            )

    view_deps, table_deps = _resolve_dependencies(
        autogen_context, declared, view_names_by_schema
    )
    topo_order = _topo_sort(declared, view_deps)

    affected_tables = _affected_table_keys(upgrade_ops)

    new_keys = {key for key in topo_order if key not in reflected}
    changed_keys = {
        key
        for key in topo_order
        if key in reflected
        and normalize_view_definition(declared[key].definition)
        != normalize_view_definition(extract_view_query(reflected[key]))
    }

    rebuild = set(changed_keys)
    rebuild.update(
        key for key in topo_order if table_deps[key] & affected_tables
    )
    # any view that directly or transitively depends on a view being
    # rebuilt has to be dropped and recreated as well
    changed = True
    while changed:
        changed = False
        for key in topo_order:
            if key not in rebuild and view_deps[key] & rebuild:
                rebuild.add(key)
                changed = True

    drops = [
        _drop_op(key, declared, reflected)
        for key in reversed(topo_order)
        if key in rebuild and key in reflected
    ]
    creates = [
        _create_op(declared[key])
        for key in topo_order
        if key in rebuild or key in new_keys
    ]

    for op in drops:
        log.info("Detected managed view %r to drop", op.view_name)
    for op in creates:
        log.info("Detected managed view %r to create", op.view_name)

    # dependent views are dropped before any table operations and
    # recreated after them
    upgrade_ops.ops[0:0] = drops
    upgrade_ops.ops.extend(creates)

    return PriorityDispatchResult.CONTINUE


def _collect_declared_views(
    autogen_context: AutogenContext,
) -> dict[_ViewKey, ManagedView]:
    declared: dict[_ViewKey, ManagedView] = {}
    for view in managed_views(autogen_context.metadata):
        if view.key in declared:
            raise ManagedViewError(
                f"managed view {view.dotted_name!r} is declared more "
                "than once"
            )
        declared[view.key] = view
    return declared


def _view_names(
    inspector: Inspector,
    view_names_by_schema: dict[str | None, set[str]],
    schema: str | None,
) -> set[str]:
    if schema not in view_names_by_schema:
        view_names_by_schema[schema] = set(
            inspector.get_view_names(schema=schema)
        )
    return view_names_by_schema[schema]


def _resolve_dependencies(
    autogen_context: AutogenContext,
    declared: dict[_ViewKey, ManagedView],
    view_names_by_schema: dict[str | None, set[str]],
) -> tuple[dict[_ViewKey, set[_ViewKey]], dict[_ViewKey, set[_ViewKey]]]:
    inspector = autogen_context.inspector
    metadata_table_keys = {
        (table.schema, table.name)
        for table in autogen_context.sorted_tables
    }
    view_deps: dict[_ViewKey, set[_ViewKey]] = {
        key: set() for key in declared
    }
    table_deps: dict[_ViewKey, set[_ViewKey]] = {
        key: set() for key in declared
    }
    for key, view in declared.items():
        for dep in view.depends_on:
            dkey = split_dependency_name(dep)
            if dkey in declared:
                view_deps[key].add(dkey)
            elif dkey in metadata_table_keys:
                table_deps[key].add(dkey)
            elif inspector.has_table(
                dkey[1], schema=dkey[0]
            ) or dkey[1] in _view_names(
                inspector, view_names_by_schema, dkey[0]
            ):
                # an existing database object that is not managed;
                # track it so that managed views are still ordered
                # after it, but never emit operations for it
                table_deps[key].add(dkey)
            else:
                raise ManagedViewError(
                    f"managed view {view.dotted_name!r} depends on {dep!r}, "
                    "which is not a declared managed view, a table in the "
                    "target metadata, or an existing database object"
                )
    return view_deps, table_deps


def _topo_sort(
    declared: dict[_ViewKey, ManagedView],
    view_deps: dict[_ViewKey, set[_ViewKey]],
) -> list[_ViewKey]:
    """Return view keys in dependency order (dependencies first).

    Raises :class:`.ManagedViewError` if a dependency cycle is found.
    """
    order: list[_ViewKey] = []
    permanent: set[_ViewKey] = set()
    temporary: list[_ViewKey] = []

    def visit(key: _ViewKey) -> None:
        if key in permanent:
            return
        if key in temporary:
            cycle = temporary[temporary.index(key) :] + [key]
            names = " -> ".join(declared[k].dotted_name for k in cycle)
            raise ManagedViewError(
                "dependency cycle detected among managed views: "
                f"{names}"
            )
        temporary.append(key)
        for dep in sorted(view_deps[key], key=lambda k: (k[0] or "", k[1])):
            visit(dep)
        temporary.pop()
        permanent.add(key)
        order.append(key)

    for key in sorted(declared, key=lambda k: (k[0] or "", k[1])):
        visit(key)
    return order


def _affected_table_keys(upgrade_ops: UpgradeOps) -> set[_ViewKey]:
    """Return the ``(schema, name)`` keys of tables that are created,
    dropped, or altered by the operations generated so far."""
    keys: set[_ViewKey] = set()
    for op in upgrade_ops.ops:
        if isinstance(
            op,
            (
                ops.CreateTableOp,
                ops.DropTableOp,
                ops.AlterTableOp,
                ops.ModifyTableOps,
            ),
        ):
            keys.add((op.schema, op.table_name))
    return keys


def _create_op(view: ManagedView) -> ops.CreateViewOp:
    return ops.CreateViewOp(
        view.name,
        view.definition,
        schema=view.schema,
        depends_on=view.depends_on,
    )


def _drop_op(
    key: _ViewKey,
    declared: dict[_ViewKey, ManagedView],
    reflected: dict[_ViewKey, str],
) -> ops.DropViewOp:
    view = declared[key]
    # keep the definition that is currently present in the database so
    # that the downgrade can restore it
    old_definition = extract_view_query(reflected[key]).strip()
    return ops.DropViewOp(
        view.name,
        schema=view.schema,
        _reverse=ops.CreateViewOp(
            view.name,
            old_definition,
            schema=view.schema,
            depends_on=view.depends_on,
        ),
    )


def setup(plugin: Plugin) -> None:
    plugin.add_autogenerate_comparator(
        _autogen_for_views,
        "schema",
        "views",
        priority=DispatchPriority.LAST,
    )

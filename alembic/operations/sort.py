"""Dependency-aware ordering of migration operations.

This module implements an *optional* ordering phase for operations
produced by autogenerate, or assembled by hand.  Operations may declare
which schema-qualified objects they create, read, drop, or rename by
implementing :meth:`.MigrateOperation.op_dependencies`.  Based on those
declarations, :func:`sort_ops` arranges the order of the operations
inside an :class:`.OpContainer` such as :class:`.UpgradeOps` or
:class:`.DowngradeOps`.

Only regions of operations that carry dependency declarations take part
in sorting.  An operation that does not declare dependencies (the
default for custom operations) is treated as an *ordering barrier*:
operations are never moved across it.  Operations that have no
dependency relationship with each other keep their original relative
order.

Container operations such as :class:`.ModifyTableOps` (which is also
what batch mode renders from) are kept intact: their boundaries are
never crossed, they are reordered only as a whole, and their contents
are sorted recursively.

When the declarations form a cycle, :class:`OpSortCycleError` is raised
describing the exact objects and operations involved.  The sorter never
tries to break a cycle by itself; deferrable work such as foreign key
creation can instead be split out into explicit post-operations (e.g.
:class:`.CreateForeignKeyOp`) by the caller or by a plugin, after which
sorting can proceed.

"""

from __future__ import annotations

import dataclasses
import heapq
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ops import MigrateOperation
    from .ops import MigrationScript
    from .ops import OpContainer


@dataclasses.dataclass(frozen=True)
class ObjectRef:
    """A reference to a schema-qualified object an operation touches."""

    kind: str
    """The kind of object, e.g. ``"table"``, ``"column"``."""

    name: str
    """The name of the object."""

    schema: str | None = None
    """The schema the object lives in, if any."""

    @classmethod
    def table(cls, name: str, schema: str | None = None) -> ObjectRef:
        """Return a reference to a table."""
        return cls("table", name, schema)

    @classmethod
    def column(
        cls,
        table_name: str,
        column_name: str,
        schema: str | None = None,
    ) -> ObjectRef:
        """Return a reference to a column within a table."""
        return cls("column", f"{table_name}.{column_name}", schema)

    @classmethod
    def index(cls, name: str, schema: str | None = None) -> ObjectRef:
        """Return a reference to an index."""
        return cls("index", name, schema)

    @classmethod
    def constraint(cls, name: str, schema: str | None = None) -> ObjectRef:
        """Return a reference to a named constraint."""
        return cls("constraint", name, schema)

    def __str__(self) -> str:
        if self.schema is not None:
            return f'{self.kind} "{self.schema}"."{self.name}"'
        else:
            return f'{self.kind} "{self.name}"'


@dataclasses.dataclass(frozen=True)
class OpDependencies:
    """Declares which schema-qualified objects an operation touches.

    :param creates: objects that did not exist before this operation
     runs; anything reading or dropping them is ordered after this
     operation.
    :param reads: objects that must exist when this operation runs;
     anything creating them is ordered before this operation, and
     anything dropping them is ordered after it.
    :param drops: objects that no longer exist once this operation has
     run; anything reading them is ordered before this operation, and
     anything re-creating them is ordered after it.
    :param renames: pairs of ``(old, new)`` :class:`.ObjectRef` for
     objects that change identity; the old identity is treated as read
     and dropped, the new identity as created, so that references to
     either identity order correctly against the rename.

    """

    creates: frozenset[ObjectRef] = frozenset()
    reads: frozenset[ObjectRef] = frozenset()
    drops: frozenset[ObjectRef] = frozenset()
    renames: tuple[tuple[ObjectRef, ObjectRef], ...] = ()


class OpSortCycleError(Exception):
    """Raised when declared operation dependencies form a cycle.

    The exception message names the concrete objects and operations
    that make up the cycle.
    """


@dataclasses.dataclass(frozen=True)
class _NormalizedDeps:
    creates: frozenset[ObjectRef]
    reads: frozenset[ObjectRef]
    drops: frozenset[ObjectRef]


def sort_ops(container: OpContainer) -> OpContainer:
    """Sort the operations within ``container`` in place.

    ``container`` is typically an :class:`.UpgradeOps` or
    :class:`.DowngradeOps` object.  Nested containers such as
    :class:`.ModifyTableOps` are sorted recursively first and are then
    treated as atomic units at the outer level; operations are never
    moved in or out of a container.

    The container is modified in place and also returned, for
    convenience.

    """
    from .ops import OpContainer as _OpContainer

    for op in container.ops:
        if isinstance(op, _OpContainer):
            sort_ops(op)
    container.ops[:] = _sort_level(list(container.ops))
    return container


def sort_migration_script(script: MigrationScript) -> MigrationScript:
    """Sort every upgrade and downgrade op list of a migration script.

    The script is modified in place and also returned, for convenience.

    """
    for upgrade_ops in script.upgrade_ops_list:
        sort_ops(upgrade_ops)
    for downgrade_ops in script.downgrade_ops_list:
        sort_ops(downgrade_ops)
    return script


def _normalize(op: MigrateOperation) -> _NormalizedDeps | None:
    deps = op.op_dependencies()
    if deps is None:
        return None
    creates = set(deps.creates)
    reads = set(deps.reads)
    drops = set(deps.drops)
    for old, new in deps.renames:
        reads.add(old)
        drops.add(old)
        creates.add(new)
    return _NormalizedDeps(
        frozenset(creates), frozenset(reads), frozenset(drops)
    )


def _sort_level(
    op_list: list[MigrateOperation],
) -> list[MigrateOperation]:
    normalized = [_normalize(op) for op in op_list]
    result = list(op_list)
    start: int | None = None
    for index in range(len(op_list) + 1):
        is_barrier = index == len(op_list) or normalized[index] is None
        if is_barrier:
            if start is not None:
                deps: dict[int, _NormalizedDeps] = {}
                for i in range(start, index):
                    dep = normalized[i]
                    assert dep is not None
                    deps[i] = dep
                result[start:index] = _topo_sort(op_list, deps, start, index)
                start = None
        elif start is None:
            start = index
    return result


def _ordering_objects(
    first: _NormalizedDeps, second: _NormalizedDeps
) -> set[ObjectRef]:
    """Return the objects requiring ``first`` to precede ``second``.

    These are the hard ordering rules: creators precede readers, and
    readers precede droppers.
    """
    return (first.creates & second.reads) | (first.reads & second.drops)


def _recreate_objects(
    first: _NormalizedDeps, second: _NormalizedDeps
) -> set[ObjectRef]:
    """Return objects that ``first`` drops and ``second`` creates.

    This is a soft rule, applied only when the hard rules say nothing
    about a pair of operations: when an object is both dropped and
    created within one stream, the drop is assumed to precede the
    (re-)create.
    """
    return first.drops & second.creates


def _topo_sort(
    op_list: list[MigrateOperation],
    deps: dict[int, _NormalizedDeps],
    start: int,
    stop: int,
) -> list[MigrateOperation]:
    members = list(range(start, stop))
    adjacent: dict[int, set[int]] = {i: set() for i in members}
    indegree: dict[int, int] = {i: 0 for i in members}
    edge_objects: dict[tuple[int, int], set[ObjectRef]] = {}
    hard_pairs: set[tuple[int, int]] = set()
    for a in members:
        for b in members:
            if a == b:
                continue
            objs = _ordering_objects(deps[a], deps[b])
            if objs:
                adjacent[a].add(b)
                indegree[b] += 1
                edge_objects[(a, b)] = objs
                hard_pairs.add((a, b))
    for a in members:
        for b in members:
            if (
                a == b
                or (a, b) in hard_pairs
                or (b, a) in hard_pairs
            ):
                continue
            objs = _recreate_objects(deps[a], deps[b])
            if objs:
                adjacent[a].add(b)
                indegree[b] += 1
                edge_objects[(a, b)] = objs

    # heapq over original positions gives a stable sort: operations
    # with no dependency relationship keep their original relative
    # order.
    ready = [i for i in members if indegree[i] == 0]
    heapq.heapify(ready)
    ordered: list[int] = []
    while ready:
        current = heapq.heappop(ready)
        ordered.append(current)
        for nxt in adjacent[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(ready, nxt)

    if len(ordered) != len(members):
        remaining = [i for i in members if indegree[i] > 0]
        cycle = _find_cycle(remaining, adjacent)
        raise OpSortCycleError(_format_cycle(op_list, cycle, edge_objects))
    return [op_list[i] for i in ordered]


def _find_cycle(nodes: list[int], adjacent: dict[int, set[int]]) -> list[int]:
    node_set = set(nodes)
    state = {node: 0 for node in nodes}
    stack: list[int] = []

    def visit(node: int) -> list[int] | None:
        state[node] = 1
        stack.append(node)
        for nxt in adjacent[node]:
            if nxt not in node_set:
                continue
            if state[nxt] == 1:
                return stack[stack.index(nxt) :]
            if state[nxt] == 0:
                found = visit(nxt)
                if found is not None:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in nodes:
        if state[node] == 0:
            found = visit(node)
            if found is not None:
                return found
    return nodes


def _describe_op(op: MigrateOperation) -> str:
    name = type(op).__name__
    for attr in (
        "table_name",
        "source_table",
        "index_name",
        "constraint_name",
    ):
        value = getattr(op, attr, None)
        if value:
            return f"{name}({value!r})"
    return name


def _format_cycle(
    op_list: list[MigrateOperation],
    cycle: list[int],
    edge_objects: dict[tuple[int, int], set[ObjectRef]],
) -> str:
    lines = [
        "Cannot determine a safe order for these operations; "
        "a dependency cycle was detected:",
        "",
    ]
    for pos, node in enumerate(cycle):
        nxt = cycle[(pos + 1) % len(cycle)]
        via = ", ".join(
            str(obj) for obj in sorted(edge_objects[(node, nxt)], key=str)
        )
        lines.append(f"  op[{node}] {_describe_op(op_list[node])}")
        lines.append(f"    must precede op[{nxt}] because of {via}")
    lines.append(f"  (which closes the cycle back at op[{cycle[0]}])")
    lines += [
        "",
        "The sorter does not attempt to break cycles automatically. "
        "Restructure the operations instead, e.g. by splitting "
        "deferrable work such as foreign key creation into explicit "
        "post-operations (CreateForeignKeyOp), then sort again.",
    ]
    return "\n".join(lines)

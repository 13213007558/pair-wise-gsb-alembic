"""Serializable migration planning utilities."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
import hashlib
import json
from typing import Any
from typing import cast
from typing import Literal

from typing_extensions import TypedDict

from .script.revision import Revision
from .script.revision import RevisionError
from .script.revision import RevisionMap
from .util import to_tuple

_Direction = Literal["upgrade", "downgrade"]
_ReasonKind = Literal["target", "down_revision", "depends_on"]


class PlanError(Exception):
    """Raised when a migration plan cannot be constructed."""


class PlanVerificationError(Exception):
    """Raised when a persisted plan no longer matches the database or graph."""


class PlanReason(TypedDict, total=False):
    kind: _ReasonKind
    target: str
    branch_label: str
    revision: str
    references: str


class PlanStep(TypedDict):
    index: int
    database: str
    operation: _Direction
    revision: str
    heads_before: list[str]
    heads_after: list[str]
    reasons: list[PlanReason]


class DatabasePlan(TypedDict):
    database: str
    operation: _Direction
    target: str | None
    branch_label: str | None
    initial_heads: list[str]
    final_heads: list[str]
    steps: list[PlanStep]


class MigrationPlan(TypedDict):
    format: str
    version: int
    atomic: bool
    graph_identity: str
    databases: list[DatabasePlan]


class DatabasePlanRequest(TypedDict, total=False):
    database: str
    current_heads: Iterable[str] | str | None
    target: str | None
    direction: _Direction
    branch_label: str | None


def revision_graph_identity(revision_map: RevisionMap) -> str:
    """Return a stable identity for revision nodes and graph edges."""

    serialized = json.dumps(
        _graph_spec(revision_map),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_migration_plan(
    revision_map: RevisionMap,
    databases: Iterable[DatabasePlanRequest | Mapping[str, Any]],
) -> MigrationPlan:
    """Build a serializable plan without running migration code.

    Planning reads the revision graph only.  It never connects to a database,
    invokes ``env.py``, or calls migration ``upgrade()`` or ``downgrade()``
    callables.  Database plans are independent; ``atomic`` is always false and
    must not be interpreted as a cross-database transaction.
    """

    _validate_graph(revision_map)
    database_plans = [
        _plan_database(revision_map, request, index)
        for index, request in enumerate(databases)
    ]
    return {
        "format": "alembic.migration-plan",
        "version": 1,
        "atomic": False,
        "graph_identity": revision_graph_identity(revision_map),
        "databases": database_plans,
    }


def verify_migration_plan(
    plan: MigrationPlan,
    current_heads: Mapping[str, Iterable[str] | str | None],
    revision_map: RevisionMap,
) -> None:
    """Verify caller-supplied current heads and graph identity against a plan.

    The caller reads current heads from its databases.  This function performs
    no I/O and compares revision metadata and topology only; migration file
    bodies are not included in graph identity.
    """

    if plan.get("format") != "alembic.migration-plan":
        raise PlanVerificationError("Unsupported migration plan format")
    if plan.get("version") != 1:
        raise PlanVerificationError("Unsupported migration plan version")
    if plan.get("atomic") is not False:
        raise PlanVerificationError("Migration plans must be non-atomic")

    expected_identity = plan.get("graph_identity")
    actual_identity = revision_graph_identity(revision_map)
    if expected_identity != actual_identity:
        raise PlanVerificationError(
            "Migration graph identity mismatch: plan has %s, current graph "
            "has %s" % (expected_identity, actual_identity)
        )

    planned = {
        database_plan["database"]: database_plan
        for database_plan in plan["databases"]
    }
    planned_names = set(planned)
    actual_names = set(current_heads)
    if planned_names != actual_names:
        raise PlanVerificationError(
            "Database set mismatch: plan has %s, current execution has %s"
            % (_format_names(planned_names), _format_names(actual_names))
        )

    for name, raw_heads in current_heads.items():
        expected = set(planned[name]["initial_heads"])
        actual = _heads_to_set(raw_heads)
        if actual != expected:
            raise PlanVerificationError(
                "Current heads for database %r do not match the plan: "
                "expected %s, found %s"
                % (name, _format_names(expected), _format_names(actual))
            )


def _graph_spec(revision_map: RevisionMap) -> dict[str, Any]:
    _ensure_map(revision_map)
    return {
        "format": "alembic.revision-graph",
        "version": 1,
        "nodes": [
            {
                "revision": rev.revision,
                "branch_labels": sorted(rev.branch_labels),
                "down_revision": sorted(rev._versioned_down_revisions),
                "depends_on": sorted(
                    rev._normalized_resolved_dependencies
                ),
            }
            for rev in _sorted_revisions(revision_map)
        ],
    }


def _validate_graph(revision_map: RevisionMap) -> None:
    already_loaded = "_revision_map" in revision_map.__dict__
    if already_loaded:
        raw_revisions = list(_sorted_revisions(revision_map))
        duplicates = sorted(
            set(getattr(revision_map, "_duplicate_revisions", ()))
        )
    else:
        raw_revisions = list(revision_map._generator())
        revisions = list(raw_revisions)

        def generator() -> Iterator[Revision]:
            yield from revisions

        revision_map._generator = generator
        duplicates = []

    revision_ids = {rev.revision for rev in raw_revisions}
    branch_labels = {
        label for rev in raw_revisions for label in rev.branch_labels
    }
    if not already_loaded:
        seen = set()
        for rev in raw_revisions:
            if rev.revision in seen:
                duplicates.append(rev.revision)
            seen.add(rev.revision)
    if duplicates:
        raise PlanError(
            "Duplicate revision(s) in migration graph: %s"
            % ", ".join(sorted(set(duplicates)))
        )

    missing = []
    if not already_loaded:
        for rev in sorted(raw_revisions, key=lambda rev: rev.revision):
            references = set(rev._versioned_down_revisions).union(
                to_tuple(rev.dependencies, default=())
            )
            for down_revision in references:
                if down_revision not in revision_ids and (
                    down_revision not in branch_labels
                ):
                    missing.append((rev.revision, down_revision))
    else:
        for rev in raw_revisions:
            for down_revision in rev._all_down_revisions:
                if revision_map._revision_map.get(down_revision) is None:
                    missing.append((rev.revision, down_revision))
    if missing:
        details = ", ".join(
            "%s references missing revision %s" % (rev_id, missing_id)
            for rev_id, missing_id in sorted(missing)
        )
        raise PlanError("Broken migration graph: %s" % details)

    try:
        _ensure_map(revision_map)
    except RevisionError as exc:
        raise PlanError(str(exc)) from exc


def _plan_database(
    revision_map: RevisionMap,
    request: DatabasePlanRequest | Mapping[str, Any],
    database_index: int,
) -> DatabasePlan:
    database = str(request.get("database", "database%d" % database_index))
    operation = request.get("direction", "upgrade")
    if operation not in ("upgrade", "downgrade"):
        raise PlanError(
            "Database %r has invalid direction %r; expected 'upgrade' or "
            "'downgrade'" % (database, operation)
        )

    raw_target = request.get("target")
    target = None if raw_target is None else str(raw_target)
    branch_label = request.get("branch_label")
    heads = _heads_to_set(request.get("current_heads"))
    _validate_current_heads(revision_map, database, heads)

    if operation == "upgrade":
        return _plan_upgrade(
            revision_map, database, heads, target, branch_label
        )
    return _plan_downgrade(
        revision_map, database, heads, target, branch_label
    )


def _plan_upgrade(
    revision_map: RevisionMap,
    database: str,
    heads: set[str],
    target: str | None,
    branch_label: str | None,
) -> DatabasePlan:
    requested_target = "heads" if target is None else target
    resolved_target = requested_target
    if branch_label is not None:
        resolved_target = "%s@%s" % (branch_label, resolved_target)

    try:
        target_revisions = cast(
            tuple[Revision, ...],
            tuple(
                rev
                for rev in revision_map._parse_upgrade_target(
                    tuple(sorted(heads)), resolved_target, True
                )
                if isinstance(rev, Revision)
            ),
        )
    except RevisionError as exc:
        raise PlanError(
            "Cannot resolve upgrade target %r for database %r: %s"
            % (resolved_target, database, exc)
        ) from exc

    target_ids = {rev.revision for rev in target_revisions}
    required = _ancestor_closure(revision_map, target_revisions)
    current_closure = _active_closure(revision_map, heads)
    extra_current = heads - required
    if extra_current:
        raise PlanError(
            "Cannot upgrade database %r to %s without also moving active "
            "head(s) outside the target ancestry: %s"
            % (resolved_target, database, _format_names(extra_current))
        )

    for target_id in target_ids:
        if target_id in current_closure and target_id not in heads:
            raise PlanError(
                "Upgrade target %r for database %r is below an active "
                "revision; use a downgrade to move to that revision"
                % (target_id, database)
            )

    planned = required - current_closure
    ordered = _order_upgrades(revision_map, planned, heads, current_closure)
    steps = _make_steps(
        revision_map,
        database,
        "upgrade",
        requested_target,
        branch_label,
        ordered,
        heads,
        target_ids,
    )
    return _database_plan(
        database,
        "upgrade",
        requested_target,
        branch_label,
        heads,
        steps,
    )


def _plan_downgrade(
    revision_map: RevisionMap,
    database: str,
    heads: set[str],
    target: str | None,
    branch_label: str | None,
) -> DatabasePlan:
    requested_target = "base" if target is None else target
    resolved_target = requested_target
    if branch_label is not None and "@" not in resolved_target:
        resolved_target = "%s@%s" % (branch_label, resolved_target)

    _validate_downgrade_target(database, heads, resolved_target)
    try:
        planned_revisions, _ = revision_map._collect_downgrade_revisions(
            tuple(sorted(heads)), resolved_target, False, False, True
        )
    except RevisionError as exc:
        raise PlanError(
            "Cannot reach downgrade target %r for database %r: %s"
            % (resolved_target, database, exc)
        ) from exc

    planned = {rev.revision for rev in planned_revisions}
    ordered = _order_downgrades(revision_map, planned, heads)
    steps = _make_steps(
        revision_map,
        database,
        "downgrade",
        requested_target,
        branch_label,
        ordered,
        heads,
        set(),
    )
    return _database_plan(
        database,
        "downgrade",
        requested_target,
        branch_label,
        heads,
        steps,
    )


def _validate_current_heads(
    revision_map: RevisionMap, database: str, heads: set[str]
) -> None:
    revisions = _revision_ids(revision_map)
    unknown = sorted(head for head in heads if head not in revisions)
    if unknown:
        raise PlanError(
            "Current head(s) for database %r are not in the migration graph: "
            "%s" % (database, ", ".join(unknown))
        )

    try:
        current = revision_map._get_all_current(tuple(sorted(heads)))
    except RevisionError as exc:
        raise PlanError(
            "Invalid current heads for database %r: %s" % (database, exc)
        ) from exc
    current_revisions = cast(
        set[Revision],
        {rev for rev in current if isinstance(rev, Revision)},
    )
    effective = {rev.revision for rev in current_revisions}

    if effective != heads:
        raise PlanError(
            "Current revisions for database %r are not an active head set: "
            "%s are ancestor revisions; active heads are %s"
            % (
                database,
                _format_names(heads - effective),
                _format_names(effective),
            )
        )


def _validate_downgrade_target(
    database: str, heads: set[str], target: str
) -> None:
    from .script.revision import _relative_destination

    match = _relative_destination.match(target)
    if not match:
        return
    branch_label, symbol, relative = match.groups()
    if int(relative) >= 0 or symbol is not None or branch_label:
        return
    if len(heads) != 1:
        raise PlanError(
            "Relative downgrade target %r for database %r is ambiguous; "
            "specify a branch label or an absolute revision"
            % (target, database)
        )


def _order_upgrades(
    revision_map: RevisionMap,
    planned: set[str],
    heads: set[str],
    active_closure: set[str],
) -> list[Revision]:
    ordered = []
    current_heads = set(heads)
    satisfied = set(active_closure)
    remaining = set(planned)

    while remaining:
        candidates = []
        for rev_id in sorted(remaining):
            rev = _get_revision(revision_map, rev_id)
            parents = set(rev._normalized_down_revisions)
            if not parents or parents <= satisfied:
                candidates.append(rev)

        if not candidates:
            raise PlanError(
                "Cannot order upgrade revisions; remaining revisions are not "
                "reachable from the active head set: %s"
                % _format_names(remaining)
            )

        rev = min(candidates, key=lambda candidate: candidate.revision)
        _apply_upgrade(current_heads, rev)
        satisfied.add(rev.revision)
        remaining.remove(rev.revision)
        ordered.append(rev)

    return ordered


def _order_downgrades(
    revision_map: RevisionMap, planned: set[str], heads: set[str]
) -> list[Revision]:
    ordered = []
    current_heads = set(heads)
    remaining = set(planned)

    while remaining:
        candidates = [
            _get_revision(revision_map, rev_id)
            for rev_id in sorted(remaining.intersection(current_heads))
        ]
        if not candidates:
            raise PlanError(
                "Cannot order downgrade revisions; no planned revision is an "
                "active head; active heads are %s; remaining revisions are %s"
                % (_format_names(current_heads), _format_names(remaining))
            )

        rev = min(candidates, key=lambda candidate: candidate.revision)
        _apply_downgrade(revision_map, current_heads, rev)
        remaining.remove(rev.revision)
        ordered.append(rev)

    return ordered


def _make_steps(
    revision_map: RevisionMap,
    database: str,
    operation: _Direction,
    target: str | None,
    branch_label: str | None,
    ordered: list[Revision],
    initial_heads: set[str],
    target_ids: set[str],
) -> list[PlanStep]:
    current_heads = set(initial_heads)
    steps = []

    for index, rev in enumerate(ordered, start=1):
        before = set(current_heads)
        if operation == "upgrade":
            reasons = _upgrade_reasons(
                rev, ordered, target_ids,
                "heads" if target is None else target, branch_label,
            )
            _apply_upgrade(current_heads, rev)
        else:
            reasons = _downgrade_reasons(
                rev, ordered,
                "base" if target is None else target, branch_label,
            )
            _apply_downgrade(revision_map, current_heads, rev)

        step: PlanStep = {
            "index": index,
            "database": database,
            "operation": operation,
            "revision": rev.revision,
            "heads_before": sorted(before),
            "heads_after": sorted(current_heads),
            "reasons": reasons,
        }
        steps.append(step)

    return steps


def _upgrade_reasons(
    rev: Revision,
    ordered: list[Revision],
    target_ids: set[str],
    target: str,
    branch_label: str | None,
) -> list[PlanReason]:
    reasons: list[PlanReason] = []
    if rev.revision in target_ids:
        reason: PlanReason = {
            "kind": "target",
            "target": target,
        }
        if branch_label is not None:
            reason["branch_label"] = branch_label
        reasons.append(reason)

    current_index = next(
        index for index, planned in enumerate(ordered) if planned == rev
    )
    for child in ordered[current_index + 1 :]:
        if rev.revision not in child._normalized_down_revisions:
            continue
        kind: _ReasonKind = (
            "depends_on"
            if rev.revision in child._normalized_resolved_dependencies
            and rev.revision not in child._versioned_down_revisions
            else "down_revision"
        )
        reasons.append(
            {
                "kind": kind,
                "revision": child.revision,
                "references": rev.revision,
            }
        )

    return reasons


def _downgrade_reasons(
    rev: Revision,
    ordered: list[Revision],
    target: str,
    branch_label: str | None,
) -> list[PlanReason]:
    current_index = next(
        index for index, planned in enumerate(ordered) if planned == rev
    )
    reasons: list[PlanReason] = []
    for child in ordered[:current_index]:
        if rev.revision not in child._normalized_down_revisions:
            continue
        kind: _ReasonKind = (
            "depends_on"
            if rev.revision in child._normalized_resolved_dependencies
            and rev.revision not in child._versioned_down_revisions
            else "down_revision"
        )
        reasons.append(
            {
                "kind": kind,
                "revision": child.revision,
                "references": rev.revision,
            }
        )

    if not reasons:
        reason: PlanReason = {
            "kind": "target",
            "target": target,
        }
        if branch_label is not None:
            reason["branch_label"] = branch_label
        reasons.append(reason)

    return reasons


def _database_plan(
    database: str,
    operation: _Direction,
    target: str | None,
    branch_label: str | None,
    initial_heads: set[str],
    steps: list[PlanStep],
) -> DatabasePlan:
    final_heads = set(initial_heads)
    if steps:
        final_heads = set(steps[-1]["heads_after"])
    return {
        "database": database,
        "operation": operation,
        "target": target,
        "branch_label": branch_label,
        "initial_heads": sorted(initial_heads),
        "final_heads": sorted(final_heads),
        "steps": steps,
    }


def _apply_upgrade(heads: set[str], rev: Revision) -> None:
    active_parents = set(rev._normalized_down_revisions).intersection(heads)
    active_versioned_parents = set(rev._versioned_down_revisions).intersection(
        active_parents
    )
    if len(active_versioned_parents) > 1:
        heads.difference_update(active_versioned_parents)
    elif active_versioned_parents:
        heads.remove(next(iter(active_versioned_parents)))
    heads.add(rev.revision)


def _apply_downgrade(
    revision_map: RevisionMap, heads: set[str], rev: Revision
) -> None:
    parents = set(rev._normalized_down_revisions)
    if not parents:
        heads.remove(rev.revision)
        return

    other_heads = heads.difference([rev.revision])
    other_ancestors = set()
    if other_heads:
        other_ancestors = {
            ancestor.revision
            for ancestor in revision_map._get_ancestor_nodes(
                revision_map.get_revisions(tuple(sorted(other_heads))),
                include_dependencies=True,
            )
        }

    destination_heads = parents.difference(other_ancestors)
    heads.remove(rev.revision)
    heads.update(destination_heads)


def _ancestor_closure(
    revision_map: RevisionMap, targets: Iterable[Revision]
) -> set[str]:
    target_tuple = tuple(targets)
    if not target_tuple:
        return set()
    return {
        rev.revision
        for rev in revision_map._get_ancestor_nodes(
            target_tuple, include_dependencies=True
        )
    }.union(rev.revision for rev in target_tuple)


def _active_closure(
    revision_map: RevisionMap, heads: set[str]
) -> set[str]:
    if not heads:
        return set()
    revisions = cast(
        tuple[Revision, ...],
        tuple(
            rev
            for rev in revision_map.get_revisions(tuple(sorted(heads)))
            if isinstance(rev, Revision)
        ),
    )
    return _ancestor_closure(
        revision_map, revisions
    )


def _ensure_map(revision_map: RevisionMap) -> None:
    revision_map._revision_map


def _revision_ids(revision_map: RevisionMap) -> set[str]:
    return {
        str(rev_id)
        for rev_id in revision_map._revision_map
        if rev_id is not None and rev_id != ()
    }


def _sorted_revisions(revision_map: RevisionMap) -> list[Revision]:
    return [
        _get_revision(revision_map, rev_id)
        for rev_id in sorted(_revision_ids(revision_map))
    ]


def _get_revision(revision_map: RevisionMap, rev_id: str) -> Revision:
    rev = revision_map.get_revision(rev_id)
    if rev is None:
        raise PlanError("Revision %r resolved to base" % rev_id)
    return rev


def _heads_to_set(value: Iterable[str] | str | None) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    heads = set(value)
    if any(not isinstance(head, str) for head in heads):
        raise PlanError("Current head identifiers must be strings")
    return heads


def _format_names(names: Iterable[str | None]) -> str:
    values = sorted(name for name in names if name is not None)
    if not values:
        return "(none)"
    return "[" + ", ".join(repr(name) for name in values) + "]"

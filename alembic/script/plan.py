from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import asdict
from dataclasses import dataclass
import hashlib
import json
from typing import Any
from typing import cast
from typing import Literal
from typing import TYPE_CHECKING
import warnings

from .revision import MultipleHeads
from .revision import ResolutionError
from .revision import Revision
from .revision import RevisionError
from .revision import RevisionMap
from .revision import _relative_destination
from .. import util

if TYPE_CHECKING:
    from .base import ScriptDirectory

Direction = Literal["upgrade", "downgrade"]


class MigrationPlanError(RevisionError):
    """A migration request cannot be represented as an executable plan."""


@dataclass(frozen=True)
class PlanReason:
    """Serializable reason for including one revision in a plan."""

    kind: str
    revision: str
    introduced_by: str | None = None
    edge: str | None = None


@dataclass(frozen=True)
class PlannedStep:
    """One migration invocation and its expected version-table state."""

    revision: str
    operation: Direction
    heads_before: tuple[str, ...]
    heads_after: tuple[str, ...]
    reasons: tuple[PlanReason, ...]


@dataclass(frozen=True)
class PlannedDatabase:
    """A plan scoped to one independent database."""

    database: str
    current_heads: tuple[str, ...]
    target_revision: str
    direction: Direction
    steps: tuple[PlannedStep, ...]


@dataclass(frozen=True)
class MigrationPlan:
    """Serializable plan for independently executed databases."""

    graph_identity: str
    databases: tuple[PlannedDatabase, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlanRequest:
    """A request for one database."""

    database: str
    current_heads: tuple[str, ...]
    target_revision: str | None
    direction: Direction


def _sorted_heads(heads: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(heads)))


def _revision_map(value: ScriptDirectory | RevisionMap) -> RevisionMap:
    if isinstance(value, RevisionMap):
        return value
    return value.revision_map


def _revision(
    revmap: RevisionMap, revision_id: str | None
) -> Revision | None:
    if revision_id is None:
        return None
    try:
        revision = revmap.get_revision(revision_id)
    except ResolutionError as exc:
        raise MigrationPlanError(str(exc)) from exc
    if revision is None:
        raise MigrationPlanError("'base' is not a revision identifier")
    return revision


def _validate_graph(revmap: RevisionMap) -> None:
    revisions = []
    duplicate_ids = set()
    seen = set()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            for revision in revmap._generator():
                revisions.append(revision)
                if revision.revision in seen:
                    duplicate_ids.add(revision.revision)
                seen.add(revision.revision)
            revmap._revision_map
        except RevisionError as exc:
            raise MigrationPlanError(str(exc)) from exc
        except KeyError as exc:
            message = "Revision %r is referenced but is not present"
            raise MigrationPlanError(message % (exc.args[0],)) from exc
    if duplicate_ids:
        raise MigrationPlanError(
            "Duplicate revision identifiers: "
            + ", ".join(sorted(duplicate_ids))
        )
    for warning in caught:
        message = str(warning.message)
        if "referenced" in message or "present more than once" in message:
            raise MigrationPlanError(message)
    revision_ids = {revision.revision for revision in revisions}
    resolved_revisions = [
        cast(
            Revision,
            revmap._revision_map[revision.revision],
        )
        for revision in revisions
    ]
    for revision in resolved_revisions:
        for parent_id in revision._all_down_revisions:
            if parent_id not in revision_ids:
                raise MigrationPlanError(
                    "Revision %r references missing revision %r"
                    % (revision.revision, parent_id)
                )


def migration_graph_identity(revision_map_or_directory: Any) -> str:
    """Return the graph-only identity.

    The identity includes revision identifiers, branch labels and declared
    down_revision/depends_on edges. Script bodies, paths and timestamps are
    intentionally excluded.
    """

    revmap = _revision_map(revision_map_or_directory)
    _validate_graph(revmap)
    graph = []
    for revision_id in revmap._revision_map:
        revision = revmap._revision_map[revision_id]
        if revision is None:
            continue
        graph.append(
            {
                "revision": revision.revision,
                "down_revision": sorted(
                    util.to_tuple(revision.down_revision, default=())
                ),
                "depends_on": sorted(revision._resolved_dependencies),
                "branch_labels": sorted(revision._orig_branch_labels),
            }
        )
    payload = json.dumps(
        sorted(graph, key=lambda item: item["revision"]),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return "sha256:" + digest


def _request(
    value: PlanRequest | Mapping[str, Any], index: int
) -> PlanRequest:
    if isinstance(value, PlanRequest):
        database = value.database
        current_values = value.current_heads
        target = value.target_revision
        direction: str | None = value.direction
    elif isinstance(value, Mapping):
        database = value.get("database", "database-%d" % index)
        current_values = tuple(value.get("current_heads", ()))
        target = value.get("target_revision")
        direction = value.get("direction")
    else:
        raise MigrationPlanError("Unsupported database request %r" % value)
    if not isinstance(database, str) or not database:
        raise MigrationPlanError("Database name must be a non-empty string")
    if target is not None and not isinstance(target, str):
        raise MigrationPlanError("target_revision must be a string or None")
    if direction not in ("upgrade", "downgrade"):
        raise MigrationPlanError(
            "direction must be 'upgrade' or 'downgrade'"
        )
    if any(not isinstance(head, str) for head in current_values):
        raise MigrationPlanError("current_heads must contain strings")
    heads = _sorted_heads(current_values)
    if len(heads) != len(list(current_values)):
        raise MigrationPlanError(
            "Current heads for database %r contain duplicates" % database
        )
    return PlanRequest(database, heads, target, cast(Direction, direction))


def _validate_current_heads(
    revmap: RevisionMap, database: str, heads: tuple[str, ...]
) -> set[Revision]:
    current = set()
    for head_id in heads:
        revision = _revision(revmap, head_id)
        assert revision is not None
        current.add(revision)
    for revision in list(current):
        ancestors = {
            ancestor
            for ancestor in revmap._get_ancestor_nodes([revision])
            if ancestor is not revision
        }
        if current.intersection(ancestors):
            raise MigrationPlanError(
                "Current heads for database %r are not branch tips: %r has "
                "another current revision as an ancestor"
                % (database, revision.revision)
            )
    return current


def _ancestor_set(
    revmap: RevisionMap, revisions: Iterable[Revision]
) -> set[Revision]:
    return set(revmap._get_ancestor_nodes(list(revisions)))


def _versioned_head_on_branch(
    revmap: RevisionMap,
    head: Revision,
    branch_label: str,
    target: Revision | None,
) -> bool:
    ancestors = set(
        revmap._get_ancestor_nodes([head], include_dependencies=False)
    )
    if target is not None:
        descendants = set(
            revmap._get_descendant_nodes(
                [target], include_dependencies=False
            )
        ).difference([target])
        return any(
            ancestor in descendants
            and branch_label in ancestor.branch_labels
            for ancestor in ancestors
        )
    return False


def _target_revisions_upgrade(
    revmap: RevisionMap,
    request: PlanRequest,
    current: set[Revision],
) -> tuple[Revision, ...]:
    target = request.target_revision
    if target is None:
        raise MigrationPlanError(
            "Upgrade target for database %r must name a revision or 'heads'"
            % request.database
        )

    relative_match = _relative_destination.match(target)
    if relative_match:
        branch_label, _symbol, relative = relative_match.groups()
        if int(relative) <= 0:
            raise MigrationPlanError(
                "Upgrade target %r for database %r does not move forward"
                % (target, request.database)
            )
        if not branch_label and len(request.current_heads) > 1:
            raise MigrationPlanError(
                "Relative upgrade target is ambiguous for database %r; "
                "current heads are %s. Name a branch or exact revision."
                % (request.database, ", ".join(request.current_heads))
            )

    try:
        if relative_match:
            targets = revmap._parse_upgrade_target(
                request.current_heads,
                target,
                assert_relative_length=True,
            )
        else:
            targets = revmap.get_revisions(target)
    except MultipleHeads as exc:
        raise MigrationPlanError(
            "Upgrade target for database %r is ambiguous: %s"
            % (request.database, ", ".join(exc.heads))
        ) from exc
    except ResolutionError as exc:
        raise MigrationPlanError(str(exc)) from exc

    if not targets:
        raise MigrationPlanError(
            "Upgrade target 'base' cannot move database %r forward"
            % request.database
        )

    return cast(tuple[Revision, ...], targets)


def _upgrade_revisions(
    revmap: RevisionMap,
    request: PlanRequest,
    current: set[Revision],
) -> set[Revision]:
    try:
        selected, _target_heads = revmap._collect_upgrade_revisions(
            request.target_revision,
            request.current_heads,
            inclusive=False,
            implicit_base=True,
            assert_relative_length=True,
        )
    except RevisionError as exc:
        raise MigrationPlanError(str(exc)) from exc
    if set(selected):
        return set(selected)
    target_revisions = set(
        _target_revisions_upgrade(revmap, request, current)
    )
    if target_revisions.difference(current):
        raise MigrationPlanError(
            "Cannot upgrade database %r to %r from current heads %s"
            % (
                request.database,
                request.target_revision,
                ", ".join(request.current_heads) or "base",
            )
        )
    return set(selected)


def _parse_downgrade_target(
    revmap: RevisionMap, request: PlanRequest
) -> tuple[str | None, Revision | None]:
    target = request.target_revision
    if target is None or target == "base":
        return None, None

    relative_match = _relative_destination.match(target)
    if relative_match:
        branch_label, symbol, relative_text = relative_match.groups()
        relative = int(relative_text)
        if relative >= 0:
            raise MigrationPlanError(
                "Downgrade target %r for database %r does not move backward"
                % (target, request.database)
            )
        if symbol is None and not branch_label:
            if len(request.current_heads) > 1:
                raise MigrationPlanError(
                    "Relative downgrade target is ambiguous for database "
                    "%r; current heads are %s. Name a branch or exact "
                    "starting revision."
                    % (
                        request.database,
                        ", ".join(request.current_heads),
                    )
                )
            if not request.current_heads:
                raise MigrationPlanError(
                    "Relative revision %s didn't produce %d migrations"
                    % (target, abs(relative))
                )
        elif symbol is None and branch_label:
            scoped = revmap.filter_for_lineage(
                request.current_heads, branch_label
            )
            if len(scoped) != 1:
                raise MigrationPlanError(
                    "Relative downgrade target is ambiguous for branch %r "
                    "on database %r; matching current heads are %s"
                    % (
                        branch_label,
                        request.database,
                        ", ".join(scoped) or "none",
                    )
                )
        try:
            _branch, revision = revmap._parse_downgrade_target(
                request.current_heads, target, True
            )
        except RevisionError as exc:
            raise MigrationPlanError(str(exc)) from exc
        if revision == "base":
            revision = None
        return branch_label, revision

    branch_label = None
    symbol = target
    if "@" in target:
        branch_label, symbol = target.split("@", 1)
    if symbol == "base":
        return branch_label, None
    if symbol == "head" or symbol == "heads":
        raise MigrationPlanError(
            "Downgrade target %r for database %r must be an ancestor, "
            "'base', or a relative revision"
            % (target, request.database)
        )
    revision = _revision(revmap, symbol)
    if branch_label is not None:
        try:
            branch = revmap._resolve_branch(branch_label)
        except ResolutionError as exc:
            raise MigrationPlanError(str(exc)) from exc
        if branch is not None and not revmap._shares_lineage(
            revision, branch.revision
        ):
            raise MigrationPlanError(
                "Revision %r is not a member of branch %r"
                % (revision.revision if revision else "base", branch_label)
            )
    return branch_label, revision


def _downgrade_revisions(
    revmap: RevisionMap,
    request: PlanRequest,
    current: set[Revision],
) -> tuple[set[Revision], set[Revision], Revision | None, str | None]:
    branch_label, target = _parse_downgrade_target(revmap, request)
    current_ids = {revision.revision for revision in current}
    active = _ancestor_set(revmap, current).union(current)
    active_ids = {revision.revision for revision in active}
    selected: set[Revision] = set()
    versioned_descendant_ids: set[str] = set()

    if target is None:
        if branch_label is None:
            starts = list(current)
        else:
            starts = [
                revision
                for revision in current
                if branch_label in revision.branch_labels
            ]
            if not starts:
                raise MigrationPlanError(
                    "Current heads for database %r do not include branch %r"
                    % (request.database, branch_label)
                )

            # A branch label first appears at its branch root, not at the
            # graph root shared before the branch diverged.
            branch_root = revmap._resolve_branch(branch_label)
            if branch_root is not None:
                versioned_ancestors = set(
                    revmap._get_ancestor_nodes(
                        [branch_root], include_dependencies=False
                    )
                )
                shared_branchpoint = any(
                    revision.is_branch_point
                    for revision in versioned_ancestors
                    if revision is not branch_root
                )
                if shared_branchpoint:
                    raise MigrationPlanError(
                        "Branch %r shares a graph base; downgrading it to "
                        "base is not direction-specific. Name the branch "
                        "point or relative target instead." % branch_label
                    )
    else:
        versioned_descendants = set(
            revmap._get_descendant_nodes(
                [target], include_dependencies=False
            )
        ).difference([target])
        versioned_descendant_ids = {
            revision.revision for revision in versioned_descendants
        }
        starts = [
            revision
            for revision in current
            if revision in versioned_descendants
        ]
        if branch_label is not None:
            starts = [
                revision
                for revision in starts
                if branch_label in revision.branch_labels
            ]
        if not starts:
            if target.revision in current_ids:
                return set(), set(), target, branch_label
            raise MigrationPlanError(
                "Cannot downgrade database %r to %r; no current head "
                "descends from that revision"
                % (request.database, target.revision)
            )

    requested = set(starts)
    todo = deque(starts)
    selected.update(starts)
    versioned_paths = set()
    for revision in starts:
        if target is None and revision._versioned_down_revisions:
            versioned_paths.add(revision)
        elif target is not None and branch_label is None:
            if revision.revision in versioned_descendant_ids:
                versioned_paths.add(revision)
        elif (
            target is not None
            and branch_label in revision.branch_labels
        ):
            versioned_paths.add(revision)
    while todo:
        revision = todo.popleft()
        on_versioned_path = revision in versioned_paths
        for child_id in revision._all_nextrev:
            child = revmap.get_revision(child_id)
            assert child is not None
            child_on_branch = (
                branch_label is None
                or _versioned_head_on_branch(
                    revmap, child, branch_label, target
                )
            )
            if child.revision in active_ids and child not in selected:
                if branch_label is None or child_on_branch:
                    selected.add(child)
                    todo.append(child)
                    if target is not None:
                        requested.add(child)
            if (
                on_versioned_path
                and child_id in revision.nextrev
                and child_on_branch
            ):
                versioned_paths.add(child)

    todo = deque(
        revision
        for revision in versioned_paths
        if revision._versioned_down_revisions
    )
    if target is not None and branch_label is not None:
        merge_seeds = [
            revision
            for revision in selected
            if revision.is_merge_point
            and any(
                branch_label in parent.branch_labels
                for parent_id in revision._versioned_down_revisions
                if (parent := revmap.get_revision(parent_id)) is not None
            )
        ]
        for merge_seed in merge_seeds:
            parent_ids = [
                parent_id
                for parent_id in merge_seed._versioned_down_revisions
                if (parent := revmap.get_revision(parent_id)) is not None
                and branch_label in parent.branch_labels
            ]
            for parent_id in parent_ids:
                parent = revmap.get_revision(parent_id)
                assert parent is not None
                if parent not in selected:
                    selected.add(parent)
        todo = deque(
            revision
            for revision in selected
            if not revision.is_merge_point
            and (
                branch_label in revision.branch_labels
                or any(
                    child_id in revision.nextrev
                    and (child := revmap.get_revision(child_id)) in selected
                    and branch_label in child.branch_labels
                    for child_id in revision.nextrev
                )
            )
        )
    while todo:
        revision = todo.popleft()
        versioned_parent_ids = revision._versioned_down_revisions
        for parent_id in versioned_parent_ids:
            parent = revmap.get_revision(parent_id)
            assert parent is not None
            if parent not in active or parent in selected:
                continue
            if target is not None:
                if parent == target:
                    continue
                if parent.revision not in versioned_descendant_ids:
                    raise MigrationPlanError(
                        "Cannot downgrade database %r to %r along a single "
                        "versioned branch while another active branch "
                        "depends on %r"
                        % (
                            request.database,
                            target.revision,
                            parent.revision,
                        )
                    )
            if branch_label is not None:
                selected_descendants = revmap._get_descendant_nodes(
                    [parent], include_dependencies=False
                )
                if not any(
                    descendant in selected
                    and (
                        branch_label in descendant.branch_labels
                        and not descendant.is_merge_point
                    )
                    for descendant in selected_descendants
                ):
                    continue
            selected.add(parent)
            todo.append(parent)

    return selected, requested, target, branch_label


def _ordered_upgrades(
    revmap: RevisionMap,
    selected: set[Revision],
    heads: set[str],
) -> list[Revision]:
    remaining = {revision.revision: revision for revision in selected}
    completed = set(heads)
    output = []
    while remaining:
        ready = []
        for revision in remaining.values():
            parents = set(revision._normalized_down_revisions)
            if parents.issubset(completed):
                ready.append(revision)
        if not ready:
            raise MigrationPlanError(
                "Cannot order revisions; required prerequisites are outside "
                "the planned path: "
                + ", ".join(sorted(remaining))
            )
        revision = min(ready, key=lambda item: item.revision)
        output.append(revision)
        del remaining[revision.revision]
        heads.difference_update(revision._normalized_down_revisions)
        heads.add(revision.revision)
        completed.add(revision.revision)
    return output


def _ordered_downgrades(selected: set[Revision]) -> list[Revision]:
    remaining = {revision.revision: revision for revision in selected}
    output = []
    while remaining:
        ready = []
        for revision in remaining.values():
            if not revision._all_nextrev.intersection(set(remaining)):
                ready.append(revision)
        if not ready:
            raise MigrationPlanError(
                "Cannot order downgrade revisions; a dependency cycle "
                "remains among: " + ", ".join(sorted(remaining))
            )
        revision = min(ready, key=lambda item: item.revision)
        output.append(revision)
        del remaining[revision.revision]
    return output


def _assert_downgrades_reachable(
    revmap: RevisionMap,
    selected: set[Revision],
    heads: set[str],
    request: PlanRequest,
) -> None:
    remaining = {revision.revision: revision for revision in selected}
    current = set(heads)
    while remaining:
        runnable = [
            remaining[revision_id]
            for revision_id in current
            if revision_id in remaining
            and not remaining[revision_id]._all_nextrev.intersection(
                set(remaining)
            )
        ]
        if not runnable:
            raise MigrationPlanError(
                "Cannot downgrade database %r to %r; selected revisions %s "
                "cannot become current heads"
                % (
                    request.database,
                    request.target_revision,
                    ", ".join(sorted(remaining)),
                )
            )
        revision = min(runnable, key=lambda item: item.revision)
        current.remove(revision.revision)
        current.update(
            _exposed_down_revisions(revmap, revision, current | {
                revision.revision
            })
        )
        del remaining[revision.revision]


def _exposed_down_revisions(
    revmap: RevisionMap, revision: Revision, heads: set[str]
) -> tuple[str, ...]:
    other_heads = heads.difference([revision.revision])
    if other_heads:
        ancestors = {
            ancestor.revision
            for ancestor in revmap._get_ancestor_nodes(
                revmap.get_revisions(other_heads), check=False
            )
        }
        return tuple(
            sorted(
                set(revision._normalized_down_revisions).difference(
                    ancestors
                )
            )
        )
    ancestors = set()
    for parent_id in revision._normalized_down_revisions:
        ancestors.update(
            parent.revision
            for parent in revmap._get_ancestor_nodes(
                [revmap.get_revision(parent_id)], check=False
            )
            if parent.revision != parent_id
        )
    return tuple(
        sorted(set(revision._normalized_down_revisions).difference(ancestors))
    )


def _apply_upgrade_head(
    heads: set[str],
    revision: Revision,
    completed: set[str],
) -> set[str]:
    next_heads = set(heads)
    parents = set(revision._normalized_down_revisions)
    versioned_parents = set(revision._versioned_down_revisions)
    if not parents:
        next_heads.add(revision.revision)
    elif not versioned_parents.intersection(heads) and (
        not versioned_parents
        or versioned_parents.intersection(completed)
    ):
        next_heads.add(revision.revision)
    elif not parents.intersection(heads):
        raise MigrationPlanError(
            "Cannot run upgrade %r; none of its prerequisites %s are "
            "current heads"
            % (
                revision.revision,
                ", ".join(sorted(parents)) or "base",
            )
        )
    else:
        next_heads.difference_update(parents)
        next_heads.add(revision.revision)
    return next_heads


def _apply_downgrade_head(
    revmap: RevisionMap,
    heads: set[str],
    revision: Revision,
) -> set[str]:
    if revision.revision not in heads:
        raise MigrationPlanError(
            "Cannot run downgrade %r; it is not a current head"
            % revision.revision
        )
    next_heads = set(heads)
    next_heads.remove(revision.revision)
    exposed = _exposed_down_revisions(revmap, revision, heads)
    next_heads.update(exposed)
    return next_heads


def _upgrade_reasons(
    selected: set[Revision], targets: set[Revision]
) -> dict[str, tuple[PlanReason, ...]]:
    reasons: dict[str, list[PlanReason]] = {}
    introduced = set()
    for revision in selected:
        for child in selected:
            versioned_parents = set(child._versioned_down_revisions)
            dependency_parents = set(child._resolved_dependencies)
            if revision.revision in versioned_parents:
                reasons.setdefault(revision.revision, []).append(
                    PlanReason(
                        "down_revision",
                        revision.revision,
                        child.revision,
                        child.revision,
                    )
                )
                introduced.add(revision.revision)
            elif revision.revision in dependency_parents:
                reasons.setdefault(revision.revision, []).append(
                    PlanReason(
                        "depends_on",
                        revision.revision,
                        child.revision,
                        child.revision,
                    )
                )
                introduced.add(revision.revision)
        if revision in targets or revision.revision not in introduced:
            reasons.setdefault(revision.revision, []).append(
                PlanReason("target", revision.revision)
            )
    return _finish_reasons(reasons, selected)


def _downgrade_reasons(
    selected: set[Revision],
    current: set[Revision],
    seeds: set[Revision],
) -> dict[str, tuple[PlanReason, ...]]:
    reasons: dict[str, list[PlanReason]] = {}
    for revision in selected:
        if revision in current:
            reasons.setdefault(revision.revision, []).append(
                PlanReason("current_head", revision.revision)
            )
        if revision in seeds and revision not in current:
            reasons.setdefault(revision.revision, []).append(
                PlanReason("target", revision.revision)
            )
        for parent_id in revision._versioned_down_revisions:
            if parent_id in {item.revision for item in selected}:
                reasons.setdefault(parent_id, []).append(
                    PlanReason(
                        "down_revision",
                        parent_id,
                        revision.revision,
                        revision.revision,
                    )
                )
        for parent_id in revision._resolved_dependencies:
            if parent_id in {item.revision for item in selected}:
                reasons.setdefault(parent_id, []).append(
                    PlanReason(
                        "depends_on",
                        parent_id,
                        revision.revision,
                        revision.revision,
                    )
                )
    return _finish_reasons(reasons, selected)


def _finish_reasons(
    reasons: dict[str, list[PlanReason]], selected: set[Revision]
) -> dict[str, tuple[PlanReason, ...]]:
    for revision in selected:
        if not reasons.get(revision.revision):
            raise MigrationPlanError(
                "Internal planning error: revision %r has no inclusion "
                "reason" % revision.revision
            )
    return {
        key: tuple(
            sorted(
                value,
                key=lambda reason: (
                    reason.kind,
                    reason.introduced_by or "",
                    reason.edge or "",
                ),
            )
        )
        for key, value in reasons.items()
    }


def _build_database_plan(
    revmap: RevisionMap, request: PlanRequest
) -> PlannedDatabase:
    current = _validate_current_heads(
        revmap, request.database, request.current_heads
    )
    heads = {head for head in request.current_heads}
    steps: list[PlannedStep] = []
    operation: Direction

    if request.direction == "upgrade":
        target_revisions = _target_revisions_upgrade(
            revmap, request, current
        )
        targets = set(target_revisions)
        selected = _upgrade_revisions(revmap, request, current)
        ordered = _ordered_upgrades(revmap, selected, set(heads))
        reasons = _upgrade_reasons(selected, targets)
        operation = "upgrade"
    else:
        selected, requested, _target, _branch = _downgrade_revisions(
            revmap, request, current
        )
        _assert_downgrades_reachable(revmap, selected, set(heads), request)
        ordered = _ordered_downgrades(selected)
        reasons = _downgrade_reasons(selected, current, requested)
        operation = "downgrade"
        target_head_ids: set[str] = set()
        ignored_heads: set[str] = set()
        selected_ids = {item.revision for item in selected}

    for revision in ordered:
        before = _sorted_heads(heads)
        if operation == "upgrade":
            heads = _apply_upgrade_head(
                heads, revision, set(step.revision for step in steps)
            )
        else:
            effective_heads = heads.difference(ignored_heads)
            if (
                _target is not None
                and _branch is not None
                and revision.is_merge_point
            ):
                exposed = set(
                    _exposed_down_revisions(
                        revmap, revision, effective_heads
                    )
                )
                target_head_ids.update(
                    parent_id
                    for parent_id in exposed
                    if parent_id in selected_ids
                    and _branch
                    in cast(
                        Revision, revmap.get_revision(parent_id)
                    ).branch_labels
                )
                ignored_heads.update(exposed.difference(selected_ids))
            if revision.revision in effective_heads:
                heads = _apply_downgrade_head(revmap, heads, revision)
            else:
                heads.remove(revision.revision)
                parents = set(
                    _exposed_down_revisions(
                        revmap, revision, effective_heads
                    )
                )
                heads.update(
                    parent_id
                    for parent_id in parents
                    if parent_id in selected_ids
                )
        steps.append(
            PlannedStep(
                revision.revision,
                operation,
                before,
                _sorted_heads(heads),
                reasons[revision.revision],
            )
        )

    return PlannedDatabase(
        request.database,
        request.current_heads,
        (
            request.target_revision or "base"
            if operation == "downgrade"
            else cast(str, request.target_revision)
        ),
        operation,
        tuple(steps),
    )


def create_migration_plan(
    revision_map_or_directory: ScriptDirectory | RevisionMap,
    requests: Iterable[PlanRequest | Mapping[str, Any]],
) -> MigrationPlan:
    """Build a serializable migration plan without running the environment.

    No database is opened, env.py is not imported or executed, and no
    migration upgrade/downgrade callable is invoked. Each returned database
    is an independent unit; the plan does not provide cross-database
    transactional atomicity.
    """

    revmap = _revision_map(revision_map_or_directory)
    _validate_graph(revmap)
    identity = migration_graph_identity(revmap)
    plan_requests = [
        _request(value, index)
        for index, value in enumerate(requests, start=1)
    ]
    names = [request.database for request in plan_requests]
    if len(names) != len(set(names)):
        raise MigrationPlanError("Database names must be unique")
    databases = tuple(
        _build_database_plan(revmap, request) for request in plan_requests
    )
    return MigrationPlan(identity, databases)


def _plan_from_mapping(
    value: MigrationPlan | Mapping[str, Any]
) -> MigrationPlan:
    if isinstance(value, MigrationPlan):
        return value
    try:
        identity = value["graph_identity"]
        raw_databases = value["databases"]
    except KeyError as exc:
        raise MigrationPlanError("Plan is missing field %r" % exc.args[0])
    if not isinstance(identity, str):
        raise MigrationPlanError("Plan graph_identity must be a string")

    databases = []
    for raw_database in raw_databases:
        steps = []
        for raw_step in raw_database["steps"]:
            reasons = tuple(
                PlanReason(**raw_reason)
                for raw_reason in raw_step["reasons"]
            )
            steps.append(
                PlannedStep(
                    raw_step["revision"],
                    raw_step["operation"],
                    tuple(raw_step["heads_before"]),
                    tuple(raw_step["heads_after"]),
                    reasons,
                )
            )
        databases.append(
            PlannedDatabase(
                raw_database["database"],
                tuple(raw_database["current_heads"]),
                raw_database["target_revision"],
                raw_database["direction"],
                tuple(steps),
            )
        )
    return MigrationPlan(identity, tuple(databases))


def check_migration_plan(
    revision_map_or_directory: ScriptDirectory | RevisionMap,
    plan: MigrationPlan | Mapping[str, Any],
    current_heads_by_database: Mapping[str, Iterable[str]],
) -> None:
    """Validate graph identity, supplied heads, and replay the full plan.

    Graph identity excludes migration script bodies. Mismatched identity,
    unknown or omitted databases, mismatched current heads, or a serialized
    plan that does not replay to the same steps all raise MigrationPlanError.
    """

    revmap = _revision_map(revision_map_or_directory)
    _validate_graph(revmap)
    candidate = _plan_from_mapping(plan)
    identity = migration_graph_identity(revmap)
    if identity != candidate.graph_identity:
        raise MigrationPlanError(
            "Migration graph identity does not match the plan; expected "
            "%s but plan contains %s"
            % (identity, candidate.graph_identity)
        )

    planned_names = [database.database for database in candidate.databases]
    supplied_names = set(current_heads_by_database)
    if set(planned_names) != supplied_names or len(planned_names) != len(
        set(planned_names)
    ):
        raise MigrationPlanError(
            "Current heads must be supplied for every planned database, and "
            "only planned databases"
        )

    requests = []
    for database in candidate.databases:
        raw_actual = current_heads_by_database[database.database]
        try:
            actual_heads = _sorted_heads(raw_actual)
        except TypeError as exc:
            raise MigrationPlanError(
                "Current heads for database %r must be iterable strings"
                % database.database
            ) from exc
        if actual_heads != database.current_heads:
            raise MigrationPlanError(
                "Current heads for database %r do not match the plan: "
                "expected %s, found %s"
                % (
                    database.database,
                    ", ".join(database.current_heads) or "base",
                    ", ".join(actual_heads) or "base",
                )
            )
        requests.append(
            PlanRequest(
                database.database,
                actual_heads,
                database.target_revision,
                database.direction,
            )
        )

    replayed = create_migration_plan(revmap, requests)
    if replayed.to_dict() != candidate.to_dict():
        raise MigrationPlanError(
            "Serialized migration plan does not match the current revision "
            "graph and database heads"
        )

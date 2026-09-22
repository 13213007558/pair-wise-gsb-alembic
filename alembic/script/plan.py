"""Compute serializable migration plans without touching a database.

This module implements the public planning API behind
:meth:`alembic.script.base.ScriptDirectory.plan_migration`.  A plan
describes, step by step, which revisions Alembic would apply for a
given ``upgrade`` or ``downgrade`` request, how the set of current
heads evolves, and which rows would be inserted, updated, or deleted
in the ``alembic_version`` table -- all without connecting to a
database or invoking any ``upgrade()`` / ``downgrade()`` functions.

"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import Iterable

from .revision import RevisionError
from .. import util
from ..runtime import migration

if TYPE_CHECKING:
    from .base import ScriptDirectory

_UPGRADE = "upgrade"
_DOWNGRADE = "downgrade"


@dataclass(frozen=True)
class VersionTableOp:
    """A single mutation of the Alembic version table.

    ``operation`` is one of ``"insert"``, ``"update"``, or
    ``"delete"``:

    * ``"insert"`` adds a new row containing ``revision``.
    * ``"update"`` rewrites the row ``from_revision`` to
      ``to_revision``.
    * ``"delete"`` removes the row containing ``revision``.

    """

    operation: str
    revision: str | None = None
    from_revision: str | None = None
    to_revision: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary for this operation."""
        return {
            "operation": self.operation,
            "revision": self.revision,
            "from_revision": self.from_revision,
            "to_revision": self.to_revision,
        }


@dataclass(frozen=True)
class PlanStep:
    """One revision movement within a :class:`.MigrationPlan`.

    Carries the identity of the revision being moved to or from, the
    distinction between its ``down_revision`` links and its
    ``depends_on`` dependencies, the set of current heads before and
    after the step, and the exact version table mutations the step performs.

    """

    revision: str
    direction: str
    doc: str | None
    down_revision: tuple[str, ...]
    depends_on: tuple[str, ...]
    branch_labels: tuple[str, ...]
    source_revisions: tuple[str, ...]
    destination_revisions: tuple[str, ...]
    heads_before: tuple[str, ...]
    heads_after: tuple[str, ...]
    version_table_ops: tuple[VersionTableOp, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary for this step."""
        return {
            "revision": self.revision,
            "direction": self.direction,
            "doc": self.doc,
            "down_revision": list(self.down_revision),
            "depends_on": list(self.depends_on),
            "branch_labels": list(self.branch_labels),
            "source_revisions": list(self.source_revisions),
            "destination_revisions": list(self.destination_revisions),
            "heads_before": list(self.heads_before),
            "heads_after": list(self.heads_after),
            "version_table_ops": [
                op.as_dict() for op in self.version_table_ops
            ],
        }


@dataclass(frozen=True)
class MigrationPlan:
    """A full, serializable description of a migration run."""

    direction: str
    target: str
    initial_heads: tuple[str, ...]
    final_heads: tuple[str, ...]
    steps: tuple[PlanStep, ...]

    def __iter__(self):
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary for this plan."""
        return {
            "direction": self.direction,
            "target": self.target,
            "initial_heads": list(self.initial_heads),
            "final_heads": list(self.final_heads),
            "steps": [step.as_dict() for step in self.steps],
        }


def _normalize_current_revs(
    script_directory: ScriptDirectory,
    current_revs: str | Iterable[str] | None,
) -> tuple[str, ...]:
    if current_revs is None:
        return ()
    if isinstance(current_revs, str):
        current_revs = (current_revs,)

    resolved = []
    revision_map = script_directory.revision_map
    for rev in current_revs:
        # resolves the identifier against the real map; raises
        # ResolutionError / MultipleHeads for unknown or ambiguous ids
        script = revision_map.get_revision(rev)
        if script is None:
            raise RevisionError(
                "%r is not a valid current revision" % (rev,)
            )
        resolved.append(script.revision)

    duplicates = sorted(
        {rev for rev in resolved if resolved.count(rev) > 1}
    )
    if duplicates:
        raise RevisionError(
            "Current revision set contains duplicates: %s"
            % util.format_as_comma(duplicates)
        )
    return tuple(resolved)


def _validate_current_heads(
    script_directory: ScriptDirectory, current: tuple[str, ...]
) -> None:
    """Ensure the given revisions form a valid version table state.

    No entry may be an ancestor of another entry: such a revision
    would never be present as a row in the version table alongside
    its descendant.

    """
    if not current:
        return
    effective = {
        script.revision
        for script in script_directory.get_all_current(current)
    }
    given = set(current)
    redundant = sorted(given.difference(effective))
    if not redundant:
        return

    revision_map = script_directory.revision_map
    others = sorted(given.difference(redundant))
    details = []
    for rev in redundant:
        implied_by = [
            other
            for other in others
            if rev
            in {
                ancestor.revision
                for ancestor in revision_map._get_ancestor_nodes(
                    revision_map.get_revisions((other,)), check=False
                )
            }
        ]
        if implied_by:
            details.append(
                "%s is already implied by %s"
                % (rev, util.format_as_comma(implied_by))
            )
        else:
            details.append("%s is not a current head" % rev)
    raise RevisionError(
        "Current revisions %s do not form a valid version table state: %s"
        % (util.format_as_comma(sorted(given)), "; ".join(details))
    )


def _version_table_ops_for_step(
    step: migration.RevisionStep, heads: set[str]
) -> tuple[VersionTableOp, ...]:
    """Simulate HeadMaintainer.update_to_step() without a database.

    Applies the same branch-aware insert/update/delete logic that the
    online migration runner uses, mutating ``heads`` in place and
    returning the operations that would run against the version table.

    """
    if step.should_delete_branch(heads):
        vers = step.delete_version_num
        heads.remove(vers)
        return (VersionTableOp("delete", revision=vers),)
    elif step.should_create_branch(heads):
        vers = step.insert_version_num
        heads.add(vers)
        return (VersionTableOp("insert", revision=vers),)
    elif step.should_merge_branches(heads):
        delete_revs, update_from_rev, update_to_rev = (
            step.merge_branch_idents(heads)
        )
        ops = [VersionTableOp("delete", revision=rev) for rev in delete_revs]
        for rev in delete_revs:
            heads.remove(rev)
        heads.remove(update_from_rev)
        heads.add(update_to_rev)
        ops.append(
            VersionTableOp(
                "update",
                from_revision=update_from_rev,
                to_revision=update_to_rev,
            )
        )
        return tuple(ops)
    elif step.should_unmerge_branches(heads):
        (
            update_from_rev,
            update_to_rev,
            insert_revs,
        ) = step.unmerge_branch_idents(heads)
        ops = [VersionTableOp("insert", revision=rev) for rev in insert_revs]
        for rev in insert_revs:
            heads.add(rev)
        heads.remove(update_from_rev)
        heads.add(update_to_rev)
        ops.append(
            VersionTableOp(
                "update",
                from_revision=update_from_rev,
                to_revision=update_to_rev,
            )
        )
        return tuple(ops)
    else:
        from_, to_ = step.update_version_num(heads)
        heads.remove(from_)
        heads.add(to_)
        return (
            VersionTableOp("update", from_revision=from_, to_revision=to_),
        )


def plan_migration(
    script_directory: ScriptDirectory,
    current_revs: str | Iterable[str] | None,
    target: str,
    direction: str = _UPGRADE,
) -> MigrationPlan:
    """Build a :class:`.MigrationPlan` against the real revision graph.

    :param script_directory: the :class:`.ScriptDirectory` whose
     revision map is traversed.
    :param current_revs: the revision identifiers currently present in
     the version table; empty means the database is at "base".
    :param target: any target accepted by the ``upgrade`` /
     ``downgrade`` commands, including revision ids, id prefixes,
     branch labels such as ``branch@head``, symbols like
     ``heads`` / ``base``, and relative forms like ``+2``
     or ``-1``.
    :param direction: ``"upgrade"`` or ``"downgrade"``.

    No database connection is established and no migration functions
    are invoked; only the revision scripts on disk are read in order to
    build the graph.

    """
    if direction not in (_UPGRADE, _DOWNGRADE):
        raise ValueError(
            "direction must be 'upgrade' or 'downgrade', got %r" % direction
        )

    current = _normalize_current_revs(script_directory, current_revs)
    _validate_current_heads(script_directory, current)

    revision_map = script_directory.revision_map
    if direction == _UPGRADE:
        scripts = reversed(
            list(
                script_directory.iterate_revisions(
                    target, current, implicit_base=True
                )
            )
        )
        revision_steps = [
            migration.MigrationStep.upgrade_from_script(revision_map, script)
            for script in scripts
        ]
    else:
        revision_steps = [
            migration.MigrationStep.downgrade_from_script(
                revision_map, script
            )
            for script in script_directory.iterate_revisions(
                current, target, select_for_downgrade=True
            )
        ]

    heads = set(current)
    initial_heads = tuple(sorted(heads))
    steps = []
    for revision_step in revision_steps:
        script = revision_step.revision
        heads_before = tuple(sorted(heads))
        ops = _version_table_ops_for_step(revision_step, heads)
        steps.append(
            PlanStep(
                revision=script.revision,
                direction=direction,
                doc=script.doc,
                down_revision=script._versioned_down_revisions,
                depends_on=util.to_tuple(script.dependencies, default=()),
                branch_labels=tuple(sorted(script.branch_labels)),
                source_revisions=revision_step.from_revisions,
                destination_revisions=revision_step.to_revisions,
                heads_before=heads_before,
                heads_after=tuple(sorted(heads)),
                version_table_ops=ops,
            )
        )

    return MigrationPlan(
        direction=direction,
        target=target,
        initial_heads=initial_heads,
        final_heads=tuple(sorted(heads)),
        steps=tuple(steps),
    )

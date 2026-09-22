"""Offline migration planning.

This module provides a public, pure-graph API to compute the sequence of
revision steps Alembic would execute to move from a given set of current
heads to a target revision, **without connecting to a database** and
**without invoking any** ``upgrade()`` **or** ``downgrade()``
**functions**.

The returned :class:`.MigrationPlan` describes, for each step, the exact
effect on the Alembic version table (inserted, deleted and updated rows)
as well as the set of heads before and after the step, so that callers
can audit a deployment - including multiple bases, branch merges and
``depends_on`` dependencies - before running it::

    from alembic.plan import plan_migrations
    from alembic.script import ScriptDirectory
    from alembic.config import Config

    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    plan = plan_migrations(script, {"1a2b3c"}, "heads", "upgrade")
    for step in plan.steps:
        print(step.revision, step.version_table_change.operation)

"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from typing import Literal
from typing import TYPE_CHECKING

from .script.revision import ResolutionError
from .util import CommandError

if TYPE_CHECKING:
    from .runtime.migration import RevisionStep
    from .script.base import ScriptDirectory

__all__ = [
    "MigrationPlan",
    "PlanStep",
    "VersionTableChange",
    "plan_migrations",
]


@dataclass(frozen=True)
class VersionTableChange:
    """The effect of a single migration step on the Alembic version table.

    A step performs at most one kind of mutation, mirroring exactly what
    :class:`.HeadMaintainer` would do against a live database:

    * ``insert`` - a new branch row is inserted (``inserts``)
    * ``delete`` - a branch row is deleted (``deletes``)
    * ``update`` - a single row is updated in place (``update``)
    * ``merge`` - rows are deleted (``deletes``) and one row is
      updated (``update``)
    * ``unmerge`` - rows are inserted (``inserts``) and one row is
      updated (``update``)
    """

    operation: Literal["insert", "delete", "update", "merge", "unmerge"]
    """The kind of mutation performed by the step."""

    inserts: tuple[str, ...] = ()
    """Revision rows inserted into the version table."""

    deletes: tuple[str, ...] = ()
    """Revision rows deleted from the version table."""

    update: tuple[str, str] | None = None
    """A ``(from_revision, to_revision)`` in-place row update, if any."""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary for this change."""
        return {
            "operation": self.operation,
            "inserts": list(self.inserts),
            "deletes": list(self.deletes),
            "update": list(self.update) if self.update is not None else None,
        }


@dataclass(frozen=True)
class PlanStep:
    """A single planned revision step."""

    revision: str
    """The revision identifier this step runs."""

    direction: Literal["upgrade", "downgrade"]
    """Whether this step runs the revision's upgrade or downgrade."""

    doc: str | None
    """The docstring of the revision script, if any."""

    down_revisions: tuple[str, ...]
    """The revision's versioned ``down_revision`` identifiers.

    This intentionally excludes ``depends_on`` dependencies, preserving
    the distinction the migration scripts themselves make.
    """

    depends_on: tuple[str, ...]
    """The revision's resolved ``depends_on`` dependencies."""

    branch_labels: tuple[str, ...]
    """Branch labels associated with the revision."""

    heads_before: tuple[str, ...]
    """Version table contents (current heads) before this step runs."""

    heads_after: tuple[str, ...]
    """Version table contents (current heads) after this step runs."""

    version_table_change: VersionTableChange
    """The mutation this step applies to the version table."""

    @property
    def is_upgrade(self) -> bool:
        """True if this step is an upgrade."""
        return self.direction == "upgrade"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary for this step."""
        return {
            "revision": self.revision,
            "direction": self.direction,
            "doc": self.doc,
            "down_revisions": list(self.down_revisions),
            "depends_on": list(self.depends_on),
            "branch_labels": list(self.branch_labels),
            "heads_before": list(self.heads_before),
            "heads_after": list(self.heads_after),
            "version_table_change": self.version_table_change.to_dict(),
        }


@dataclass(frozen=True)
class MigrationPlan:
    """A full, serializable migration plan."""

    direction: Literal["upgrade", "downgrade"]
    """The direction of the plan."""

    target: str
    """The target revision identifier as requested."""

    initial_heads: tuple[str, ...]
    """The validated set of current heads the plan starts from."""

    final_heads: tuple[str, ...]
    """The set of heads once all steps have been applied."""

    steps: tuple[PlanStep, ...]
    """The ordered steps of the plan."""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary for this plan."""
        return {
            "direction": self.direction,
            "target": self.target,
            "initial_heads": list(self.initial_heads),
            "final_heads": list(self.final_heads),
            "steps": [step.to_dict() for step in self.steps],
        }


def plan_migrations(
    script_directory: ScriptDirectory,
    current_revisions: Iterable[str] | None,
    target: str,
    direction: Literal["upgrade", "downgrade"] = "upgrade",
) -> MigrationPlan:
    """Produce an offline migration plan.

    Computes, against the real migration graph of the given
    :class:`.ScriptDirectory`, the steps Alembic would run to move from
    ``current_revisions`` to ``target``, without touching a
    database or running any migration functions.  The given
    ``script_directory`` and ``current_revisions`` collection are
    not modified.

    :param script_directory: the :class:`.ScriptDirectory` to plan
     against.
    :param current_revisions: an iterable of revision identifiers
     representing the current contents of the version table.  ``None``
     or an empty iterable means the database is at "base".  The
     collection is only read, never mutated.
    :param target: the target revision.  Supports everything the
     ``upgrade`` / ``downgrade`` commands support, including
     ``"heads"``, ``"head"``, ``"base"``, partial revision
     identifiers, branch labels such as ``"mybranch@head"`` and
     relative targets such as ``"+2"`` or ``"-1"``.
    :param direction: ``"upgrade"`` or ``"downgrade"``.
    :return: a :class:`.MigrationPlan`.
    :raises alembic.util.CommandError: if the direction is invalid, the
     target is ambiguous or unreachable from the current heads, any
     revision cannot be resolved, the graph contains a cycle, or
     ``current_revisions`` does not form a valid set of heads.

    """
    if direction not in ("upgrade", "downgrade"):
        raise CommandError(
            "direction must be 'upgrade' or 'downgrade'; got %r" % (direction,)
        )

    # copy and de-duplicate, preserving order; the caller's collection
    # is never mutated
    current = tuple(dict.fromkeys(current_revisions or ()))

    heads = _validate_current_heads(script_directory, current)

    revision_steps: list[RevisionStep]
    try:
        if direction == "upgrade":
            revision_steps = script_directory._upgrade_revs(
                target, current  # type: ignore[arg-type]
            )
        else:
            revision_steps = script_directory._downgrade_revs(
                target, current  # type: ignore[arg-type]
            )
    except CommandError as err:
        # ScriptDirectory flattens every ResolutionError into a generic
        # "Can't locate revision" message; when the underlying cause
        # pinpoints the conflict (e.g. an ambiguous partial identifier),
        # surface that message instead so the caller can locate it.
        cause = err.__cause__
        if (
            isinstance(cause, ResolutionError)
            and cause.args
            and str(cause.args[0]).startswith("Multiple")
        ):
            raise CommandError(str(cause.args[0])) from cause
        raise

    steps = []
    current_heads = set(heads)
    for revision_step in revision_steps:
        heads_before = tuple(sorted(current_heads))
        change = _simulate_version_table_change(revision_step, current_heads)
        current_heads = _apply_version_table_change(current_heads, change)
        script = revision_step.revision
        steps.append(
            PlanStep(
                revision=script.revision,
                direction=direction,
                doc=script.doc,
                down_revisions=tuple(script._versioned_down_revisions),
                depends_on=tuple(script._resolved_dependencies),
                branch_labels=tuple(sorted(script.branch_labels)),
                heads_before=heads_before,
                heads_after=tuple(sorted(current_heads)),
                version_table_change=change,
            )
        )

    return MigrationPlan(
        direction=direction,
        target=target,
        initial_heads=tuple(sorted(heads)),
        final_heads=tuple(sorted(current_heads)),
        steps=tuple(steps),
    )


def _validate_current_heads(
    script_directory: ScriptDirectory, current: tuple[str, ...]
) -> set[str]:
    """Validate that ``current`` is a plausible version table state.

    Returns the current heads as a set of resolved revision identifiers.

    """
    with script_directory._catch_revision_errors():
        scripts = script_directory.get_revisions(current)

        revision_map = script_directory.revision_map
        head_set = {script.revision for script in scripts}

        # rows in the version table always form an antichain in the
        # revision graph: whenever a revision is applied, any row that is
        # one of its ancestors (following both down_revision and
        # depends_on edges) is consumed by an update, merge or delete.
        # A state where one row is an ancestor of another could never
        # have been produced by real migrations.
        for script in scripts:
            ancestors = {
                ancestor.revision
                for ancestor in revision_map._get_ancestor_nodes([script])
            } - {script.revision}
            conflicts = sorted(ancestors & head_set)
            if conflicts:
                raise CommandError(
                    "Current revision state %s is not a valid set of "
                    "heads: revision(s) %s are ancestors of '%s' and "
                    "cannot be present in the version table at the "
                    "same time"
                    % (
                        _format_revisions(sorted(head_set)),
                        _format_revisions(conflicts),
                        script.revision,
                    )
                )

    return head_set


def _simulate_version_table_change(
    step: RevisionStep, heads: set[str]
) -> VersionTableChange:
    """Compute the version table mutation of a single step.

    Mirrors :meth:`.HeadMaintainer.update_to_step` exactly, without
    touching a database.

    """
    if step.should_delete_branch(heads):
        return VersionTableChange(
            operation="delete", deletes=(step.delete_version_num,)
        )
    elif step.should_create_branch(heads):
        return VersionTableChange(
            operation="insert", inserts=(step.insert_version_num,)
        )
    elif step.should_merge_branches(heads):
        delete_revs, update_from_rev, update_to_rev = (
            step.merge_branch_idents(heads)
        )
        return VersionTableChange(
            operation="merge",
            deletes=tuple(delete_revs),
            update=(update_from_rev, update_to_rev),
        )
    elif step.should_unmerge_branches(heads):
        update_from_rev, update_to_rev, insert_revs = (
            step.unmerge_branch_idents(heads)
        )
        return VersionTableChange(
            operation="unmerge",
            inserts=tuple(insert_revs),
            update=(update_from_rev, update_to_rev),
        )
    else:
        from_rev, to_rev = step.update_version_num(heads)
        return VersionTableChange(
            operation="update", update=(from_rev, to_rev)
        )


def _apply_version_table_change(
    heads: set[str], change: VersionTableChange
) -> set[str]:
    new_heads = set(heads)
    new_heads.difference_update(change.deletes)
    new_heads.update(change.inserts)
    if change.update is not None:
        from_rev, to_rev = change.update
        new_heads.discard(from_rev)
        new_heads.add(to_rev)
    return new_heads


def _format_revisions(revisions: list[str]) -> str:
    return ", ".join("'%s'" % revision for revision in revisions)

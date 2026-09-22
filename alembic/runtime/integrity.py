# mypy: allow-untyped-defs, allow-incomplete-defs, allow-untyped-calls
# mypy: no-warn-return-any, allow-any-generics

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import TYPE_CHECKING

from sqlalchemy import Column
from sqlalchemy import MetaData
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import Text

from .. import util
from ..util import sqla_compat

if TYPE_CHECKING:
    from collections.abc import Collection

    from ..script.base import Script
    from ..script.base import ScriptDirectory
    from .migration import MigrationContext
    from .migration import RevisionStep

log = logging.getLogger(__name__)


class RevisionIntegrityError(util.CommandError):
    """Raised when recorded revision digests do not match the
    current state of migration files."""


def compute_revision_digest(script: Script) -> str:
    """Compute a content digest for a single revision script.

    The digest covers the raw bytes of the migration file as well as the
    revision's graph declarations (``revision``, ``down_revision``,
    ``depends_on`` and ``branch_labels``) as parsed from the file.
    Auxiliary modules imported by the migration file are not tracked.

    """
    if not os.path.exists(script.path):
        raise RevisionIntegrityError(
            "Migration file for revision %s does not exist: %s"
            % (script.revision, script.path)
        )

    digest = hashlib.sha256()
    with open(script.path, "rb") as file_:
        for chunk in iter(lambda: file_.read(65536), b""):
            digest.update(chunk)

    graph_declaration = {
        "revision": script.revision,
        "down_revision": sorted(
            util.to_tuple(script.down_revision, default=())
        ),
        "depends_on": sorted(
            util.to_tuple(script.dependencies, default=())
        ),
        "branch_labels": sorted(script.branch_labels or ()),
    }
    digest.update(
        json.dumps(
            graph_declaration, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    return digest.hexdigest()


def executed_closure(
    script_directory: ScriptDirectory, heads: Collection[str]
) -> set[Script]:
    """Return the full set of revision scripts that are considered
    applied for the given collection of current database heads.

    This includes every ancestor of every head, traversing both
    ``down_revision`` and ``depends_on`` links, so that multiple heads,
    merge revisions and cross-branch dependencies are all represented.

    """
    try:
        head_scripts = script_directory.get_revisions(tuple(heads))
        closure = set(head_scripts)
        closure.update(
            script_directory.revision_map._get_ancestor_nodes(
                list(head_scripts), include_dependencies=True
            )
        )
    except RevisionIntegrityError:
        raise
    except Exception as err:
        raise RevisionIntegrityError(
            "Can't determine the executed revision closure for "
            "current heads %s; the revision graph is incomplete "
            "or inconsistent: %s" % (util.format_as_comma(heads), err)
        ) from err
    return closure


class RevisionDigestStore:
    """Persist per-revision content digests in a table alongside the
    Alembic version table.

    One row is stored for every revision that has been successfully
    applied, not only for the current head(s); the collection of rows
    therefore represents the full executed closure of the database.

    """

    def __init__(self, context: MigrationContext) -> None:
        self.context = context
        self.table = Table(
            "%s_digests" % context.version_table,
            MetaData(),
            Column("revision", String(255), primary_key=True),
            Column("digest", String(64), nullable=False),
            Column("path", Text(), nullable=True),
            schema=context.version_table_schema,
        )

    def has_table(self) -> bool:
        assert self.context.connection is not None
        return sqla_compat._connectable_has_table(
            self.context.connection,
            self.table.name,
            self.context.version_table_schema,
        )

    def ensure_table(self) -> None:
        with sqla_compat._ensure_scope_for_ddl(self.context.connection):
            assert self.context.connection is not None
            self.table.create(self.context.connection, checkfirst=True)

    def load(self) -> dict[str, str]:
        """Return a mapping of revision id to recorded digest."""
        if not self.has_table():
            return {}
        assert self.context.connection is not None
        return {
            row[0]: row[1]
            for row in self.context.connection.execute(
                select(self.table.c.revision, self.table.c.digest)
            )
        }

    def record(self, script: Script) -> None:
        """Record the digest of a successfully applied revision."""
        digest = compute_revision_digest(script)
        log.info("Recording digest for revision %s", script.revision)
        self.context.impl._exec(
            self.table.insert().values(
                revision=script.revision,
                digest=digest,
                path=script.path,
            )
        )

    def remove(self, revisions: Collection[str]) -> None:
        """Remove records for revisions that have been downgraded.

        This must only be called after the corresponding downgrade has
        succeeded.
        """
        if not revisions:
            return
        log.info("Removing digests for revisions %s", revisions)
        self.context.impl._exec(
            self.table.delete().where(
                self.table.c.revision.in_(tuple(revisions))
            )
        )

    def purge(self) -> None:
        """Remove all recorded digests."""
        if self.has_table():
            self.context.impl._exec(self.table.delete())

    def replace_all(self, scripts: Collection[Script]) -> None:
        """Replace all records with digests for the given scripts."""
        self.ensure_table()
        self.purge()
        for script in scripts:
            self.record(script)

    def record_step(self, step: RevisionStep) -> None:
        """Update records for a successfully executed migration step.

        For upgrades, the digest of the applied revision is recorded;
        for downgrades, the record of the reverted revision is removed.
        """
        if step.is_upgrade:
            self.record(step.revision)
        else:
            self.remove((step.revision.revision,))


def verify_executed_history(
    context: MigrationContext, script_directory: ScriptDirectory
) -> None:
    """Verify recorded digests against the current migration files.

    Compares the digests recorded for the database's executed revision
    closure against digests freshly computed from the migration files
    on disk.  Raises :class:`.RevisionIntegrityError` on any mismatch;
    recorded digests are never modified by this function.

    """
    store = RevisionDigestStore(context)
    heads = context.get_current_heads()
    stored = store.load()

    if not heads:
        if stored:
            raise RevisionIntegrityError(
                "Digest records exist for revision(s) %s, but the "
                "version table '%s' has no current revisions"
                % (util.format_as_comma(sorted(stored)), context.version_table)
            )
        return

    closure = executed_closure(script_directory, heads)
    closure_ids = {script.revision for script in closure}

    missing = closure_ids.difference(stored)
    if missing:
        raise RevisionIntegrityError(
            "No digest recorded for executed revision(s) %s; if this "
            "database was migrated before digest verification was "
            "enabled, record the current history explicitly using "
            "'alembic adopt_digests'"
            % util.format_as_comma(sorted(missing))
        )

    extra = set(stored).difference(closure_ids)
    if extra:
        raise RevisionIntegrityError(
            "Digest records exist for revision(s) %s which are not part "
            "of the executed history implied by current heads %s"
            % (
                util.format_as_comma(sorted(extra)),
                util.format_as_comma(heads),
            )
        )

    for script in sorted(closure, key=lambda script: script.revision):
        digest = compute_revision_digest(script)
        if stored[script.revision] != digest:
            raise RevisionIntegrityError(
                "Digest mismatch for revision %s (file %s); the "
                "migration file or its revision graph declarations "
                "have changed since the revision was applied. "
                "Recorded digests are never overwritten automatically; "
                "restore the file or re-adopt the history explicitly "
                "with 'alembic adopt_digests'"
                % (script.revision, script.path)
            )


def adopt_executed_history(
    context: MigrationContext, script_directory: ScriptDirectory
) -> set[Script]:
    """Record digests for the database's current executed closure.

    This is the explicit, first-time adoption entry point for databases
    that were migrated before digest verification was enabled.  The
    full executed closure is computed from the current heads; if any
    revision is missing, any migration file is absent, or the revision
    graph is incomplete, adoption is refused and nothing is recorded.

    """
    heads = context.get_current_heads()
    if heads:
        closure = executed_closure(script_directory, heads)
    else:
        closure = set()

    # compute all digests up front; any failure (e.g. a missing file)
    # refuses the adoption before anything is written
    for script in closure:
        compute_revision_digest(script)

    store = RevisionDigestStore(context)
    # wrap in a migration-level transaction so that the records are
    # committed even when the surrounding environment does not maintain
    # an outer transaction (e.g. backends without transactional DDL)
    with context.begin_transaction(_per_migration=True):
        store.replace_all(closure)
    return closure

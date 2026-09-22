# mypy: allow-untyped-defs, allow-incomplete-defs, allow-untyped-calls
# mypy: no-warn-return-any, allow-any-generics

"""Optional content checksum tracking for applied migration scripts.

This module implements an opt-in integrity mechanism:  when enabled,
Alembic records a content checksum for every revision it applies, and
verifies the recorded history against the current script files before
running any further upgrade() / downgrade() functions.

The checksum for a revision covers the raw bytes of the migration file
as well as the revision's graph declarations (down_revision,
branch_labels and depends_on).  Auxiliary modules imported by
migration scripts are not tracked.

"""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING

from sqlalchemy import Column
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import delete
from sqlalchemy import select

from .. import util
from ..script.revision import RevisionError
from ..util import sqla_compat

if TYPE_CHECKING:
    from ..script.base import Script
    from .migration import MigrationContext


class ChecksumError(util.CommandError):
    """Base class for revision checksum errors."""


class RevisionChecksumMismatch(ChecksumError):
    """A recorded checksum does not match the current migration script."""

    def __init__(self, revision: str, path: str) -> None:
        super().__init__(
            "Checksum mismatch for revision '%s' (file '%s'); the "
            "migration script has changed since it was applied. "
            "Recorded checksums are never overwritten automatically; "
            "restore the original script or re-adopt the checksum "
            "history explicitly." % (revision, path)
        )
        self.revision = revision
        self.path = path


def compute_revision_checksum(script: Script) -> str:
    """Return the content checksum for a single revision script.

    The checksum covers the raw bytes of the script file as well as a
    canonical rendering of the revision's graph declarations, that is
    revision, down_revision, depends_on and branch_labels.

    """
    try:
        with open(script.path, "rb") as file_:
            file_bytes = file_.read()
    except OSError as err:
        raise ChecksumError(
            "Cannot compute checksum for revision '%s': script file "
            "'%s' is not readable: %s" % (script.revision, script.path, err)
        ) from err

    graph_declaration = {
        "revision": script.revision,
        "down_revision": sorted(
            util.to_tuple(script.down_revision, default=())
        ),
        "depends_on": sorted(
            util.to_tuple(script.dependencies, default=())
        ),
        "branch_labels": sorted(script.branch_labels),
    }
    digest = hashlib.sha256()
    digest.update(file_bytes)
    digest.update(b"\x00alembic-graph\x00")
    digest.update(
        json.dumps(graph_declaration, sort_keys=True).encode("utf-8")
    )
    return digest.hexdigest()


class RevisionChecksumStore:
    """Persist and verify per-revision content checksums.

    Checksums are stored one row per applied revision in a table
    companion to the Alembic version table, so that the full set of
    executed revisions - including multiple heads, merge revisions and
    revisions applied via depends_on - is tracked individually.

    """

    def __init__(self, context: MigrationContext) -> None:
        self.context = context
        table_name = context.opts.get("checksum_table")
        if not table_name:
            table_name = "%s_checksums" % context.version_table
        self.checksum_table_name = table_name
        self.checksum_table_schema = context.version_table_schema
        self._table = Table(
            table_name,
            MetaData(),
            Column("revision", String(255), primary_key=True),
            Column("checksum", String(64), nullable=False),
            schema=self.checksum_table_schema,
        )

    @property
    def _connection(self):
        connection = self.context.connection
        assert connection is not None
        return connection

    def _has_table(self) -> bool:
        return sqla_compat._connectable_has_table(
            self._connection,
            self.checksum_table_name,
            self.checksum_table_schema,
        )

    def ensure_table(self) -> None:
        with sqla_compat._ensure_scope_for_ddl(self._connection):
            self._table.create(self._connection, checkfirst=True)

    def load(self) -> dict[str, str]:
        """Return the recorded {revision: checksum} mapping."""
        if not self._has_table():
            return {}
        return {
            row[0]: row[1]
            for row in self._connection.execute(
                select(self._table.c.revision, self._table.c.checksum)
            )
        }

    def record_script(self, script: Script) -> None:
        """Record the checksum for a revision that was just applied."""
        self.ensure_table()
        checksum = compute_revision_checksum(script)
        self._connection.execute(
            self._table.delete().where(
                self._table.c.revision == script.revision
            )
        )
        self._connection.execute(
            self._table.insert().values(
                revision=script.revision, checksum=checksum
            )
        )

    def discard(self, revision: str) -> None:
        """Remove the record for a revision that was just downgraded."""
        if not self._has_table():
            return
        self._connection.execute(
            self._table.delete().where(self._table.c.revision == revision)
        )

    def _script_directory(self):
        script_directory = self.context.script
        if script_directory is None:
            raise ChecksumError(
                "Revision checksum verification requires a script "
                "directory; the migration context was configured "
                "without one."
            )
        return script_directory

    def verify(self) -> None:
        """Check all recorded checksums against the current scripts.

        Does nothing if no checksum history has been recorded yet.
        Raises :class:`.ChecksumError` if a recorded revision can no
        longer be resolved or its file is missing, and
        :class:`.RevisionChecksumMismatch` if a checksum does not
        match.  Recorded checksums are never modified here.

        """
        records = self.load()
        if not records:
            return
        script_directory = self._script_directory()
        for revision in sorted(records):
            try:
                script = script_directory.get_revision(revision)
            except Exception as err:
                raise ChecksumError(
                    "Checksum history refers to revision '%s', which "
                    "is not present in the script directory: %s"
                    % (revision, err)
                ) from err
            if script is None:
                raise ChecksumError(
                    "Checksum history refers to revision '%s', which "
                    "is not present in the script directory" % (revision,)
                )
            if not os.path.exists(script.path):
                raise ChecksumError(
                    "Checksum history refers to revision '%s', but its "
                    "script file '%s' does not exist"
                    % (revision, script.path)
                )
            actual = compute_revision_checksum(script)
            if actual != records[revision]:
                raise RevisionChecksumMismatch(revision, script.path)

    def adopt(self, heads: tuple[str, ...]) -> int:
        """Record checksums for the full executed closure of heads.

        This is the explicit, first-time adoption entry point for
        databases that have no checksum history yet.  The adoption is
        refused, leaving any existing records untouched, if a revision
        is missing from the script directory, a script file is missing,
        or the revision graph is incomplete.

        """
        script_directory = self._script_directory()
        try:
            targets = list(script_directory.get_revisions(heads))
            closure = set(
                script_directory.revision_map._get_ancestor_nodes(
                    targets, include_dependencies=True
                )
            )
        except KeyError as err:
            raise ChecksumError(
                "Cannot adopt checksum history: the revision graph is "
                "incomplete; revision '%s' is not present in the "
                "script directory" % (err.args[0],)
            ) from err
        except (RevisionError, util.CommandError) as err:
            raise ChecksumError(
                "Cannot adopt checksum history: %s" % (err,)
            ) from err

        # compute all checksums before mutating any state, so that a
        # refused adoption (e.g. a missing script file) leaves existing
        # records untouched
        records = [
            (script.revision, compute_revision_checksum(script))
            for script in closure
        ]
        self.ensure_table()
        self._connection.execute(self._table.delete())
        for revision, checksum in records:
            self._connection.execute(
                self._table.insert().values(
                    revision=revision, checksum=checksum
                )
            )
        return len(records)

import os

from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text

from alembic import command
from alembic import util
from alembic.migration import MigrationContext
from alembic.runtime.checksum import ChecksumError
from alembic.runtime.checksum import RevisionChecksumMismatch
from alembic.runtime.checksum import compute_revision_checksum
from alembic.script import ScriptDirectory
from alembic.testing import eq_
from alembic.testing.assertions import expect_raises_message
from alembic.testing.env import _get_staging_directory
from alembic.testing.env import _sqlite_testing_config
from alembic.testing.env import clear_staging_env
from alembic.testing.env import env_file_fixture
from alembic.testing.env import staging_env
from alembic.testing.fixtures import TestBase

CHECKSUM_TABLE = "alembic_version_checksums"

_ENV_TEMPLATE = """
from sqlalchemy import engine_from_config

engine = engine_from_config(
    config.get_section(config.config_ini_section),
    prefix='sqlalchemy.')

connection = engine.connect()

context.configure(
    connection=connection,
    %(configure_opts)s
)

try:
    with context.begin_transaction():
        context.run_migrations()
finally:
    connection.close()
    engine.dispose()
"""


def _env_file(verify):
    opts = "verify_revision_checksums=True," if verify else ""
    env_file_fixture(_ENV_TEMPLATE % {"configure_opts": opts})


class RevisionChecksumTest(TestBase):
    def setUp(self):
        self.env = staging_env()
        self.cfg = _sqlite_testing_config()
        _env_file(verify=True)

    def tearDown(self):
        clear_staging_env()

    def _write_rev(
        self,
        rev,
        down_revision=None,
        depends_on=None,
        fail_downgrade=False,
    ):
        versions = os.path.join(
            _get_staging_directory(), "scripts", "versions"
        )
        os.makedirs(versions, exist_ok=True)
        decls = "revision = %r\ndown_revision = %r\n" % (
            rev,
            down_revision,
        )
        if depends_on is not None:
            decls += "depends_on = %r\n" % (depends_on,)
        if fail_downgrade:
            downgrade_body = 'raise Exception("downgrade fails")'
        else:
            downgrade_body = 'op.execute("DROP TABLE tbl_%s")' % rev
        path = os.path.join(versions, "%s_.py" % rev)
        with open(path, "w") as file_:
            file_.write(
                '"Rev %s"\n' % rev
                + decls
                + "\nfrom alembic import op\n\n\n"
                + "def upgrade():\n"
                + '    op.execute("CREATE TABLE tbl_%s (id INTEGER)")\n'
                % rev
                + "\n\ndef downgrade():\n"
                + "    %s\n" % downgrade_body
            )
        return path

    def _tamper(self, path):
        with open(path, "ab") as file_:
            file_.write(b"\n# tampered after the fact\n")
        pyc_path = util.pyc_file_from_path(path)
        if pyc_path and os.path.exists(pyc_path):
            os.unlink(pyc_path)

    def _engine(self):
        return create_engine(self.cfg.get_main_option("sqlalchemy.url"))

    def _current_heads(self):
        engine = self._engine()
        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            heads = set(context.get_current_heads())
        engine.dispose()
        return heads

    def _recorded_checksums(self):
        engine = self._engine()
        with engine.connect() as conn:
            if not inspect(conn).has_table(CHECKSUM_TABLE):
                return {}
            records = dict(
                conn.execute(
                    text(
                        "SELECT revision, checksum FROM %s" % CHECKSUM_TABLE
                    )
                ).all()
            )
        engine.dispose()
        return records

    def _has_table(self, table):
        engine = self._engine()
        with engine.connect() as conn:
            has_table = inspect(conn).has_table(table)
        engine.dispose()
        return has_table

    def _expected_checksums(self, *revs):
        script = ScriptDirectory.from_config(self.cfg)
        return {
            rev: compute_revision_checksum(script.get_revision(rev))
            for rev in revs
        }

    def test_upgrade_records_full_closure_merge_and_depends_on(self):
        self._write_rev("a1")
        self._write_rev("b1", down_revision="a1")
        self._write_rev("b2", down_revision="a1")
        self._write_rev("m1", down_revision=("b1", "b2"))
        self._write_rev("d1", down_revision="a1", depends_on="m1")

        command.upgrade(self.cfg, "heads")

        # d1 depends_on m1, so its version row subsumes m1's row
        eq_(self._current_heads(), {"d1"})
        # but every executed revision, including the merge revision and
        # the revision linked via depends_on, is recorded individually
        eq_(
            self._recorded_checksums(),
            self._expected_checksums("a1", "b1", "b2", "m1", "d1"),
        )

    def test_tampered_ancestor_stops_upgrade_before_any_migration(self):
        a1_path = self._write_rev("a1")
        self._write_rev("b1", down_revision="a1")
        self._write_rev("b2", down_revision="a1")
        command.upgrade(self.cfg, "heads")
        eq_(self._current_heads(), {"b1", "b2"})
        before = self._recorded_checksums()
        eq_(set(before), {"a1", "b1", "b2"})

        # modify a non-head ancestor file, then add a new revision
        # that would otherwise be applied
        self._tamper(a1_path)
        self._write_rev("c1", down_revision="b1")

        with expect_raises_message(
            RevisionChecksumMismatch,
            r"Checksum mismatch for revision 'a1' \(file '[^']*a1_\.py'\)",
        ):
            command.upgrade(self.cfg, "heads")

        # the pending migration function was never invoked
        assert not self._has_table("tbl_c1")
        eq_(self._current_heads(), {"b1", "b2"})
        # recorded checksums were not overwritten
        eq_(self._recorded_checksums(), before)

        # downgrade is blocked in the same way, before any migration
        # function runs
        with expect_raises_message(
            RevisionChecksumMismatch,
            r"Checksum mismatch for revision 'a1'",
        ):
            command.downgrade(self.cfg, "base")

        assert self._has_table("tbl_b1")
        assert self._has_table("tbl_b2")
        eq_(self._current_heads(), {"b1", "b2"})
        eq_(self._recorded_checksums(), before)

    def test_downgrade_removes_record(self):
        self._write_rev("a1")
        self._write_rev("b1", down_revision="a1")
        self._write_rev("c1", down_revision="b1")

        command.upgrade(self.cfg, "head")
        eq_(set(self._recorded_checksums()), {"a1", "b1", "c1"})

        command.downgrade(self.cfg, "b1")
        eq_(self._recorded_checksums(), self._expected_checksums("a1", "b1"))

        command.downgrade(self.cfg, "base")
        eq_(self._recorded_checksums(), {})

        command.upgrade(self.cfg, "head")
        eq_(set(self._recorded_checksums()), {"a1", "b1", "c1"})

    def test_failed_downgrade_keeps_record(self):
        self._write_rev("a1")
        self._write_rev("b1", down_revision="a1", fail_downgrade=True)

        command.upgrade(self.cfg, "head")
        before = self._recorded_checksums()
        eq_(set(before), {"a1", "b1"})

        with expect_raises_message(Exception, "downgrade fails"):
            command.downgrade(self.cfg, "base")

        eq_(self._recorded_checksums(), before)
        eq_(self._current_heads(), {"b1"})
        assert self._has_table("tbl_b1")

    def test_verify_without_history_is_noop(self):
        # simulate a database migrated before checksums were enabled:
        # no checksum table exists, verification passes silently
        _env_file(verify=False)
        self._write_rev("a1")
        self._write_rev("b1", down_revision="a1")
        command.upgrade(self.cfg, "head")
        eq_(self._recorded_checksums(), {})

        # enabling verification does not disturb the existing database;
        # newly applied revisions are recorded from here on
        _env_file(verify=True)
        self._write_rev("c1", down_revision="b1")
        command.upgrade(self.cfg, "head")
        eq_(self._recorded_checksums(), self._expected_checksums("c1"))

    def test_stamp_synchronizes_records(self):
        self._write_rev("a1")
        self._write_rev("b1", down_revision="a1")

        command.stamp(self.cfg, "head")
        eq_(self._recorded_checksums(), self._expected_checksums("a1", "b1"))

        command.stamp(self.cfg, "base")
        eq_(self._recorded_checksums(), {})

    def test_missing_revision_in_history(self):
        self._write_rev("a1")
        b1_path = self._write_rev("b1", down_revision="a1")
        command.upgrade(self.cfg, "head")

        os.unlink(b1_path)
        pyc_path = util.pyc_file_from_path(b1_path)
        if pyc_path and os.path.exists(pyc_path):
            os.unlink(pyc_path)

        with expect_raises_message(
            ChecksumError,
            "Checksum history refers to revision 'b1'",
        ):
            command.upgrade(self.cfg, "head")


class AdoptChecksumsTest(TestBase):
    def setUp(self):
        self.env = staging_env()
        self.cfg = _sqlite_testing_config()
        _env_file(verify=False)

    def tearDown(self):
        clear_staging_env()

    _write_rev = RevisionChecksumTest._write_rev
    _tamper = RevisionChecksumTest._tamper
    _engine = RevisionChecksumTest._engine
    _current_heads = RevisionChecksumTest._current_heads
    _recorded_checksums = RevisionChecksumTest._recorded_checksums
    _has_table = RevisionChecksumTest._has_table
    _expected_checksums = RevisionChecksumTest._expected_checksums

    def _insert_version_row(self, rev):
        engine = self._engine()
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:r)"),
                {"r": rev},
            )
        engine.dispose()

    def test_adopt_existing_database(self):
        self._write_rev("a1")
        self._write_rev("b1", down_revision="a1")
        self._write_rev("b2", down_revision="a1")
        command.upgrade(self.cfg, "heads")
        eq_(self._recorded_checksums(), {})

        command.adopt_checksums(self.cfg)
        eq_(
            self._recorded_checksums(),
            self._expected_checksums("a1", "b1", "b2"),
        )

        # once adopted, verification is enforced
        _env_file(verify=True)
        command.downgrade(self.cfg, "base")
        eq_(self._recorded_checksums(), {})

    def test_adopt_refuses_unknown_revision(self):
        self._write_rev("a1")
        command.upgrade(self.cfg, "head")
        self._insert_version_row("zzz")

        with expect_raises_message(
            ChecksumError, "Cannot adopt checksum history"
        ):
            command.adopt_checksums(self.cfg)

        eq_(self._recorded_checksums(), {})

    def test_adopt_refuses_incomplete_graph(self):
        self._write_rev("b1", down_revision="missing1")
        command.ensure_version(self.cfg)
        self._insert_version_row("b1")

        with expect_raises_message(
            ChecksumError,
            "Cannot adopt checksum history: the revision graph is "
            "incomplete; revision 'missing1'",
        ):
            command.adopt_checksums(self.cfg)

        eq_(self._recorded_checksums(), {})

    def test_adopt_then_tamper_detected(self):
        self._write_rev("a1")
        b2_path = self._write_rev("b2", down_revision="a1")
        command.upgrade(self.cfg, "head")
        command.adopt_checksums(self.cfg)

        self._tamper(b2_path)
        _env_file(verify=True)
        with expect_raises_message(
            RevisionChecksumMismatch,
            r"Checksum mismatch for revision 'b2' \(file '[^']*b2_\.py'\)",
        ):
            command.upgrade(self.cfg, "head")

        eq_(self._current_heads(), {"b2"})

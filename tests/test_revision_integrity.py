import os

from sqlalchemy import text

from alembic import command
from alembic.runtime.integrity import compute_revision_digest
from alembic.runtime.integrity import RevisionIntegrityError
from alembic.script import ScriptDirectory
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing.env import _sqlite_file_db
from alembic.testing.env import _sqlite_testing_config
from alembic.testing.env import clear_staging_env
from alembic.testing.env import env_file_fixture
from alembic.testing.env import staging_env
from alembic.testing.env import write_script
from alembic.testing.fixtures import TestBase
from alembic.util.sqla_compat import _connectable_has_table


def _rev_content(rev, down_rev, token, depends_on=None, branch_labels=None):
    down = "None" if down_rev is None else repr(down_rev)
    depends = "None" if depends_on is None else repr(depends_on)
    labels = "None" if branch_labels is None else repr(branch_labels)
    return f'''\
"{token}"
revision = {rev!r}
down_revision = {down}
depends_on = {depends}
branch_labels = {labels}

from alembic import op


def upgrade():
    op.execute("CREATE TABLE {token} (id INTEGER)")


def downgrade():
    op.execute("DROP TABLE {token}")
'''


class _BranchEnvFixture:
    """a -> b -> c  and  a -> d (two heads, c and d)"""

    def _setup_branches(self):
        script = ScriptDirectory.from_config(self.cfg)
        self.a = script.generate_revision(
            "aaaa", "a", refresh=True, head="base"
        ).revision
        write_script(script, self.a, _rev_content(self.a, None, "a_tbl"))
        self.b = script.generate_revision(
            "bbbb", "b", head=self.a
        ).revision
        write_script(script, self.b, _rev_content(self.b, self.a, "b_tbl"))
        self.c = script.generate_revision(
            "cccc", "c", head=self.b
        ).revision
        write_script(script, self.c, _rev_content(self.c, self.b, "c_tbl"))
        self.d = script.generate_revision(
            "dddd", "d", head=self.a, splice=True
        ).revision
        write_script(script, self.d, _rev_content(self.d, self.a, "d_tbl"))


class DigestVerificationTest(_BranchEnvFixture, TestBase):
    def setUp(self):
        self.env = staging_env()
        self.cfg = _sqlite_testing_config()
        self.cfg.set_main_option("verify_revision_digests", "true")
        # transactional_ddl so that a failed migration run rolls back
        # as a whole, including digest record changes
        env_file_fixture(
            """
from sqlalchemy import engine_from_config

engine = engine_from_config(
    config.get_section(config.config_ini_section), prefix="sqlalchemy."
)
connection = engine.connect()
context.configure(connection=connection, transactional_ddl=True)

try:
    with context.begin_transaction():
        context.run_migrations()
finally:
    connection.close()
    engine.dispose()
"""
        )
        self._setup_branches()
        self.bind = _sqlite_file_db()

    def tearDown(self):
        clear_staging_env()

    def _table_names(self):
        with self.bind.connect() as conn:
            return {
                row[0]
                for row in conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

    def _stored_digests(self):
        with self.bind.connect() as conn:
            if not _connectable_has_table(
                conn, "alembic_version_digests", None
            ):
                return {}
            return {
                row[0]: row[1]
                for row in conn.exec_driver_sql(
                    "SELECT revision, digest FROM alembic_version_digests"
                )
            }

    def _current_heads(self):
        with self.bind.connect() as conn:
            if not _connectable_has_table(conn, "alembic_version", None):
                return ()
            return tuple(
                row[0]
                for row in conn.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                )
            )

    def test_no_records_without_verify_opt(self):
        self.cfg.set_main_option("verify_revision_digests", "false")
        command.upgrade(self.cfg, "heads")
        eq_(self._stored_digests(), {})
        assert "alembic_version_digests" not in self._table_names()

    def test_records_full_closure_multi_head(self):
        command.upgrade(self.cfg, "heads")
        eq_(set(self._current_heads()), {self.c, self.d})

        stored = self._stored_digests()
        eq_(set(stored), {self.a, self.b, self.c, self.d})

        script = ScriptDirectory.from_config(self.cfg)
        for rev_id, digest in stored.items():
            eq_(
                digest,
                compute_revision_digest(script.get_revision(rev_id)),
            )

    def test_downgrade_removes_records(self):
        command.upgrade(self.cfg, "heads")
        command.downgrade(self.cfg, self.a)
        eq_(set(self._stored_digests()), {self.a})
        command.downgrade(self.cfg, "base")
        eq_(self._stored_digests(), {})

    def test_tampered_ancestor_fails_before_any_migration(self):
        command.upgrade(self.cfg, "heads")
        stored_before = self._stored_digests()

        # modify a non-head ancestor file, leaving revision and
        # down_revision identifiers intact
        script = ScriptDirectory.from_config(self.cfg)
        ancestor_path = script.get_revision(self.b).path
        with open(ancestor_path, "a") as file_:
            file_.write("\n# tampered after the fact\n")

        try:
            command.downgrade(self.cfg, "base")
            assert False, "expected RevisionIntegrityError"
        except RevisionIntegrityError as err:
            # the error reports both the revision and the file path
            assert self.b in str(err), str(err)
            assert ancestor_path in str(err), str(err)

        # no migration function was invoked: all tables and the
        # version table are untouched, and recorded digests were
        # not overwritten
        assert {"a_tbl", "b_tbl", "c_tbl", "d_tbl"}.issubset(
            self._table_names()
        )
        eq_(set(self._current_heads()), {self.c, self.d})
        eq_(self._stored_digests(), stored_before)

    def test_tampered_graph_declaration_fails(self):
        script = ScriptDirectory.from_config(self.cfg)
        write_script(
            script,
            self.b,
            _rev_content(
                self.b, self.a, "b_tbl", branch_labels="b_label"
            ),
        )
        command.upgrade(self.cfg, "heads")

        # change only the branch label declaration; the graph structure
        # itself remains valid
        script = ScriptDirectory.from_config(self.cfg)
        write_script(
            script,
            self.b,
            _rev_content(
                self.b, self.a, "b_tbl", branch_labels="other_label"
            ),
        )

        assert_raises_message(
            RevisionIntegrityError,
            "Digest mismatch for revision %s" % self.b,
            command.downgrade,
            self.cfg,
            "base",
        )

    def test_failed_downgrade_keeps_records(self):
        # b's downgrade is broken from the start, so the digest
        # recorded at upgrade time still matches the file
        script = ScriptDirectory.from_config(self.cfg)
        write_script(
            script,
            self.b,
            f'''\
"b"
revision = {self.b!r}
down_revision = {self.a!r}

from alembic import op


def upgrade():
    op.execute("CREATE TABLE b_tbl (id INTEGER)")


def downgrade():
    op.execute("THIS IS NOT SQL")
''',
        )

        command.upgrade(self.cfg, "heads")

        from sqlalchemy import exc as sqla_exc

        try:
            command.downgrade(self.cfg, "base")
            assert False, "expected downgrade to fail"
        except sqla_exc.OperationalError:
            pass

        # the whole run rolled back; no records were removed
        eq_(set(self._stored_digests()), {self.a, self.b, self.c, self.d})
        eq_(set(self._current_heads()), {self.c, self.d})

    def test_existing_db_requires_explicit_adoption(self):
        # migrate with verification off, simulating a pre-existing
        # database with no digest history
        self.cfg.set_main_option("verify_revision_digests", "false")
        command.upgrade(self.cfg, "heads")
        eq_(self._stored_digests(), {})

        self.cfg.set_main_option("verify_revision_digests", "true")
        assert_raises_message(
            RevisionIntegrityError,
            "No digest recorded for executed revision",
            command.downgrade,
            self.cfg,
            "base",
        )

        command.adopt_digests(self.cfg)
        eq_(set(self._stored_digests()), {self.a, self.b, self.c, self.d})

        # verification passes now
        command.downgrade(self.cfg, self.a)
        eq_(set(self._stored_digests()), {self.a})

    def test_adopt_refuses_unknown_revision(self):
        self.cfg.set_main_option("verify_revision_digests", "false")
        command.upgrade(self.cfg, "heads")

        with self.bind.begin() as conn:
            conn.execute(
                text("INSERT INTO alembic_version VALUES ('not_a_rev')")
            )

        assert_raises_message(
            RevisionIntegrityError,
            "Can't determine the executed revision closure",
            command.adopt_digests,
            self.cfg,
        )
        eq_(self._stored_digests(), {})

    def test_adopt_refuses_incomplete_graph(self):
        self.cfg.set_main_option("verify_revision_digests", "false")
        command.upgrade(self.cfg, "heads")

        # remove a non-head ancestor file; the graph can no longer
        # resolve the executed closure
        script = ScriptDirectory.from_config(self.cfg)
        os.unlink(script.get_revision(self.b).path)

        assert_raises_message(
            RevisionIntegrityError,
            "Can't determine the executed revision closure",
            command.adopt_digests,
            self.cfg,
        )
        eq_(self._stored_digests(), {})


class MergeAndDependsOnTest(TestBase):
    """digest records track merge revisions and depends_on closures"""

    def setUp(self):
        self.env = staging_env()
        self.cfg = _sqlite_testing_config()
        self.cfg.set_main_option("verify_revision_digests", "true")
        env_file_fixture(
            """
from sqlalchemy import engine_from_config

engine = engine_from_config(
    config.get_section(config.config_ini_section), prefix="sqlalchemy."
)
connection = engine.connect()
context.configure(connection=connection)

try:
    with context.begin_transaction():
        context.run_migrations()
finally:
    connection.close()
    engine.dispose()
"""
        )
        script = ScriptDirectory.from_config(self.cfg)

        # branch one: a1 -> a2
        self.a1 = script.generate_revision(
            "a1a1", "a1", refresh=True, head="base"
        ).revision
        write_script(script, self.a1, _rev_content(self.a1, None, "a1_tbl"))
        self.a2 = script.generate_revision(
            "a2a2", "a2", head=self.a1
        ).revision
        write_script(script, self.a2, _rev_content(self.a2, self.a1, "a2_tbl"))

        # branch two: b1 -> b2, where b2 depends_on a2
        self.b1 = script.generate_revision(
            "b1b1", "b1", refresh=True, head="base"
        ).revision
        write_script(script, self.b1, _rev_content(self.b1, None, "b1_tbl"))
        self.b2 = script.generate_revision(
            "b2b2", "b2", head=self.b1, depends_on=self.a2
        ).revision
        write_script(
            script,
            self.b2,
            _rev_content(
                self.b2, self.b1, "b2_tbl", depends_on=self.a2
            ),
        )

        # merge of the two heads
        self.m1 = script.generate_revision(
            "m1m1", "m1", head=(self.a2, self.b2)
        ).revision
        write_script(
            script,
            self.m1,
            _rev_content(self.m1, (self.a2, self.b2), "m1_tbl"),
        )

        self.bind = _sqlite_file_db()

    def tearDown(self):
        clear_staging_env()

    def _stored_digests(self):
        with self.bind.connect() as conn:
            if not _connectable_has_table(
                conn, "alembic_version_digests", None
            ):
                return {}
            return {
                row[0]: row[1]
                for row in conn.exec_driver_sql(
                    "SELECT revision, digest FROM alembic_version_digests"
                )
            }

    def test_merge_and_depends_on_closure(self):
        command.upgrade(self.cfg, "heads")

        # the full executed set is recorded, including the merge
        # revision and the cross-branch dependency
        eq_(
            set(self._stored_digests()),
            {self.a1, self.a2, self.b1, self.b2, self.m1},
        )

        command.downgrade(self.cfg, "base")
        eq_(self._stored_digests(), {})

    def test_partial_downgrade_keeps_dependency_records(self):
        command.upgrade(self.cfg, "heads")

        # downgrade only the merge revision; everything else,
        # including the depends_on closure, stays recorded
        command.downgrade(self.cfg, self.b2)
        eq_(
            set(self._stored_digests()),
            {self.a1, self.a2, self.b1, self.b2},
        )


class ConfigureFlagTest(_BranchEnvFixture, TestBase):
    """verification can be enabled directly from env.py via
    context.configure()"""

    def setUp(self):
        self.env = staging_env()
        self.cfg = _sqlite_testing_config()
        env_file_fixture(
            """
from sqlalchemy import engine_from_config

engine = engine_from_config(
    config.get_section(config.config_ini_section), prefix="sqlalchemy."
)
connection = engine.connect()
context.configure(connection=connection, verify_revision_digests=True)

try:
    with context.begin_transaction():
        context.run_migrations()
finally:
    connection.close()
    engine.dispose()
"""
        )
        self._setup_branches()
        self.bind = _sqlite_file_db()

    def tearDown(self):
        clear_staging_env()

    def test_configure_flag_enables_verification(self):
        command.upgrade(self.cfg, "heads")
        with self.bind.connect() as conn:
            assert _connectable_has_table(
                conn, "alembic_version_digests", None
            )
            stored = {
                row[0]: row[1]
                for row in conn.exec_driver_sql(
                    "SELECT revision, digest FROM alembic_version_digests"
                )
            }
        eq_(set(stored), {self.a, self.b, self.c, self.d})

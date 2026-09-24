"""Tests for explicitly declared managed views (alembic.views)."""

from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import Table
from sqlalchemy import text
from sqlalchemy.sql.elements import quoted_name

from alembic import command
from alembic import op
from alembic.autogenerate import compare_metadata
from alembic.autogenerate import produce_migrations
from alembic.autogenerate.api import render_python_code
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations import ops
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing.env import _sqlite_file_db
from alembic.testing.env import _sqlite_testing_config
from alembic.testing.env import clear_staging_env
from alembic.testing.env import env_file_fixture
from alembic.testing.env import staging_env
from alembic.testing.fixtures import op_fixture
from alembic.testing.fixtures import TestBase
from alembic.views import extract_view_query
from alembic.views import ManagedView
from alembic.views import managed_views
from alembic.views import ManagedViewError
from alembic.views import normalize_view_definition
from alembic.views import register_view


class ManagedViewDeclarationTest(TestBase):
    def test_register_and_retrieve(self):
        m = MetaData()
        v1 = ManagedView("v1", "SELECT 1", metadata=m)
        v2 = ManagedView("v2", "SELECT 2")
        register_view(m, v2)
        eq_(managed_views(m), [v1, v2])
        eq_(managed_views([m]), [v1, v2])
        eq_(managed_views(None), [])

    def test_duplicate_registration_rejected(self):
        m = MetaData()
        ManagedView("v1", "SELECT 1", metadata=m)
        assert_raises_message(
            ManagedViewError,
            "managed view 'v1' is already registered",
            ManagedView,
            "v1",
            "SELECT 2",
            metadata=m,
        )

    def test_definition_required(self):
        assert_raises_message(
            ManagedViewError,
            "requires a non-empty SQL query definition",
            ManagedView,
            "v1",
            "   ",
        )

    def test_depends_on_normalization(self):
        m = MetaData()
        t = Table("t1", m, Column("id", Integer))
        t2 = Table("t2", m, Column("id", Integer), schema="s1")
        v1 = ManagedView("v1", "SELECT 1")
        v2 = ManagedView(
            "v2", "SELECT 1", depends_on=[t, t2, v1, "other", "s2.third"]
        )
        eq_(v2.depends_on, ("t1", "s1.t2", "v1", "other", "s2.third"))

    def test_invalid_dependency_type(self):
        assert_raises_message(
            ManagedViewError,
            "view dependencies must be strings, ManagedView objects, "
            "or Table objects",
            ManagedView,
            "v1",
            "SELECT 1",
            depends_on=[42],
        )

    def test_key_and_dotted_name(self):
        v = ManagedView(
            quoted_name("My View", True), "SELECT 1", schema="s1"
        )
        eq_(v.key, ("s1", "My View"))
        eq_(v.dotted_name, "s1.My View")


class NormalizeViewDefinitionTest(TestBase):
    def test_whitespace_collapsed(self):
        eq_(
            normalize_view_definition("SELECT  a,\n\t b  FROM   t"),
            "SELECT a, b FROM t",
        )

    def test_string_constants_untouched(self):
        eq_(
            normalize_view_definition(
                "SELECT  a  FROM t  WHERE x = 'keep  these   spaces'"
            ),
            "SELECT a FROM t WHERE x = 'keep  these   spaces'",
        )

    def test_escaped_quote_in_string(self):
        eq_(
            normalize_view_definition("SELECT 'it''s  x'  FROM  t"),
            "SELECT 'it''s  x' FROM t",
        )

    def test_quoted_identifier_untouched(self):
        eq_(
            normalize_view_definition(
                'SELECT  "odd  col"  FROM  "my  table"'
            ),
            'SELECT "odd  col" FROM "my  table"',
        )

    def test_trailing_semicolons_dropped(self):
        eq_(
            normalize_view_definition("SELECT 1 FROM t ;  ;;"),
            "SELECT 1 FROM t",
        )

    def test_case_is_significant(self):
        eq_(
            normalize_view_definition("select 1") ==
            normalize_view_definition("SELECT 1"),
            False,
        )


class ExtractViewQueryTest(TestBase):
    def test_plain(self):
        eq_(
            extract_view_query("CREATE VIEW v1 AS SELECT id FROM t1"),
            "SELECT id FROM t1",
        )

    def test_case_insensitive_prefix(self):
        eq_(
            extract_view_query("create view v1 as select 1"),
            "select 1",
        )

    def test_if_not_exists(self):
        eq_(
            extract_view_query(
                "CREATE VIEW IF NOT EXISTS v1 AS SELECT 1"
            ),
            "SELECT 1",
        )

    def test_quoted_name(self):
        eq_(
            extract_view_query('CREATE VIEW "my view" AS SELECT 1'),
            "SELECT 1",
        )

    def test_schema_qualified_name(self):
        eq_(
            extract_view_query("CREATE VIEW main.v1 AS SELECT 1"),
            "SELECT 1",
        )

    def test_non_create_view_returned_as_is(self):
        eq_(extract_view_query("SELECT 1"), "SELECT 1")


class ViewOpTest(TestBase):
    def test_create_view(self):
        context = op_fixture("sqlite")
        op.create_view("v1", "SELECT id FROM t1", depends_on=["t1"])
        context.assert_("CREATE VIEW v1 AS SELECT id FROM t1")

    def test_create_view_schema(self):
        context = op_fixture("sqlite")
        op.create_view("v1", "SELECT 1", schema="main")
        context.assert_("CREATE VIEW main.v1 AS SELECT 1")

    def test_create_view_quoted_name(self):
        context = op_fixture("sqlite")
        op.create_view(quoted_name("my view", True), "SELECT 1")
        context.assert_('CREATE VIEW "my view" AS SELECT 1')

    def test_drop_view(self):
        context = op_fixture("sqlite")
        op.drop_view("v1")
        context.assert_("DROP VIEW v1")

    def test_drop_view_schema(self):
        context = op_fixture("sqlite")
        op.drop_view("v1", schema="main")
        context.assert_("DROP VIEW main.v1")

    def test_drop_view_quoted_name(self):
        context = op_fixture("sqlite")
        op.drop_view(quoted_name("my view", True))
        context.assert_('DROP VIEW "my view"')

    def test_create_view_invalid_definition_error(self):
        engine = create_engine("sqlite://")
        mc = MigrationContext.configure(engine.connect())
        operations = Operations(mc)
        assert_raises_message(
            ManagedViewError,
            "could not create view 'vbad' from the given definition",
            operations.invoke,
            ops.CreateViewOp("vbad", "SELECT FROM WHERE"),
        )

    def test_reverse_roundtrip(self):
        create = ops.CreateViewOp("v1", "SELECT 1", depends_on=["t1"])
        drop = create.reverse()
        eq_(drop.view_name, "v1")
        eq_(drop.reverse().definition, "SELECT 1")

    def test_drop_without_reverse_is_irreversible(self):
        drop = ops.DropViewOp("v1")
        assert_raises_message(
            NotImplementedError,
            "cannot reverse a drop_view operation without the "
            "original view definition",
            drop.reverse,
        )


class ViewRenderTest(TestBase):
    def test_render_create_view(self):
        up = ops.UpgradeOps(
            [ops.CreateViewOp("v1", "SELECT id FROM t1", depends_on=["t1"])]
        )
        eq_(
            render_python_code(up),
            "# ### commands auto generated by Alembic - please adjust! ###\n"
            "    op.create_view('v1', 'SELECT id FROM t1', "
            "depends_on=['t1'])\n"
            "    # ### end Alembic commands ###",
        )

    def test_render_create_view_schema(self):
        up = ops.UpgradeOps(
            [ops.CreateViewOp("v1", "SELECT 1", schema="main")]
        )
        eq_(
            render_python_code(up),
            "# ### commands auto generated by Alembic - please adjust! ###\n"
            "    op.create_view('v1', 'SELECT 1', schema='main')\n"
            "    # ### end Alembic commands ###",
        )

    def test_render_drop_view(self):
        up = ops.UpgradeOps([ops.DropViewOp("v1", schema="main")])
        eq_(
            render_python_code(up),
            "# ### commands auto generated by Alembic - please adjust! ###\n"
            "    op.drop_view('v1', schema='main')\n"
            "    # ### end Alembic commands ###",
        )


def _flatten(ops_list):
    for o in ops_list:
        if hasattr(o, "ops"):
            yield from _flatten(o.ops)
        else:
            yield o


class ViewAutogenTest(TestBase):
    __only_on__ = "sqlite"

    def setUp(self):
        self.engine = create_engine("sqlite://")

    def tearDown(self):
        self.engine.dispose()

    def _metadata(self, table_extra=(), v1_def="SELECT id, name FROM t1"):
        m = MetaData()
        t = Table(
            "t1",
            m,
            Column("id", Integer),
            Column("name", Integer),
            *table_extra,
        )
        v1 = ManagedView("v1", v1_def, depends_on=[t], metadata=m)
        ManagedView("v2", "SELECT id FROM v1", depends_on=[v1], metadata=m)
        return m

    def _produce(self, metadata):
        mc = MigrationContext.configure(self.engine.connect())
        return produce_migrations(mc, metadata)

    def _apply(self, ops_list):
        mc = MigrationContext.configure(self.engine.connect())
        operations = Operations(mc)
        for o in _flatten(ops_list):
            operations.invoke(o)

    def _create_base_objects(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE t1 (id INTEGER, name INTEGER)"))
            conn.execute(
                text("CREATE VIEW v1 AS SELECT id, name FROM t1")
            )
            conn.execute(text("CREATE VIEW v2 AS SELECT id FROM v1"))

    def test_unmanaged_views_never_touched(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE t1 (id INTEGER, name INTEGER)"))
            conn.execute(
                text("CREATE VIEW custom AS SELECT id FROM t1")
            )
        m = MetaData()
        Table("t1", m, Column("id", Integer), Column("name", Integer))
        mc = MigrationContext.configure(self.engine.connect())
        eq_(compare_metadata(mc, m), [])
        # the unmanaged view is still there and no drop was generated
        eq_(
            inspect(self.engine).get_view_names(),
            ["custom"],
        )

    def test_new_views_created_in_dependency_order(self):
        ms = self._produce(self._metadata())
        flat = list(_flatten(ms.upgrade_ops.ops))
        view_ops = [
            o
            for o in flat
            if isinstance(o, (ops.CreateViewOp, ops.DropViewOp))
        ]
        eq_(
            [(type(o).__name__, o.view_name) for o in view_ops],
            [("CreateViewOp", "v1"), ("CreateViewOp", "v2")],
        )

    def test_identical_definition_no_ops(self):
        self._create_base_objects()
        ms = self._produce(self._metadata())
        eq_(ms.upgrade_ops.as_diffs(), [])

    def test_formatting_only_difference_no_ops(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE t1 (id INTEGER, name INTEGER)"))
            conn.execute(
                text(
                    "CREATE VIEW v1 AS SELECT   id,\n name    FROM   t1"
                )
            )
            conn.execute(text("CREATE VIEW v2 AS SELECT id FROM v1"))
        ms = self._produce(self._metadata())
        eq_(ms.upgrade_ops.as_diffs(), [])

    def test_string_constant_difference_produces_op(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE t1 (id INTEGER, name INTEGER)"))
            conn.execute(
                text("CREATE VIEW v1 AS SELECT id, 'a  b' AS name FROM t1")
            )
            conn.execute(text("CREATE VIEW v2 AS SELECT id FROM v1"))
        m = self._metadata(v1_def="SELECT id, 'a b' AS name FROM t1")
        ms = self._produce(m)
        creates = [
            o
            for o in _flatten(ms.upgrade_ops.ops)
            if isinstance(o, ops.CreateViewOp)
        ]
        # whitespace inside the string constant is significant, so the
        # view is rebuilt
        assert creates, "expected view rebuild ops"

    def test_changed_definition_rebuilds_dependents_in_order(self):
        self._create_base_objects()
        ms = self._produce(
            self._metadata(v1_def="SELECT id, name, id + 1 AS extra FROM t1")
        )
        flat = list(_flatten(ms.upgrade_ops.ops))
        eq_(
            [(type(o).__name__, o.view_name) for o in flat],
            [
                ("DropViewOp", "v2"),
                ("DropViewOp", "v1"),
                ("CreateViewOp", "v1"),
                ("CreateViewOp", "v2"),
            ],
        )

    def test_downgrade_restores_previous_definition(self):
        self._create_base_objects()
        ms = self._produce(
            self._metadata(v1_def="SELECT id, name, id + 1 AS extra FROM t1")
        )
        down_creates = [
            o
            for o in _flatten(ms.downgrade_ops.ops)
            if isinstance(o, ops.CreateViewOp)
        ]
        eq_(
            [(o.view_name, o.definition) for o in down_creates],
            [
                ("v1", "SELECT id, name FROM t1"),
                ("v2", "SELECT id FROM v1"),
            ],
        )

    def test_table_change_drops_views_first_recreates_last(self):
        self._create_base_objects()
        m = self._metadata(table_extra=(Column("extra", Integer),))
        ms = self._produce(m)
        flat = list(_flatten(ms.upgrade_ops.ops))
        kinds = [type(o).__name__ for o in flat]
        eq_(kinds[:2], ["DropViewOp", "DropViewOp"])
        eq_(kinds[-2:], ["CreateViewOp", "CreateViewOp"])
        eq_(flat[0].view_name, "v2")
        eq_(flat[1].view_name, "v1")
        assert any(isinstance(o, ops.AddColumnOp) for o in flat)

    def test_dependency_cycle_error(self):
        m = MetaData()
        ManagedView("v1", "SELECT 1", depends_on=["v2"], metadata=m)
        ManagedView("v2", "SELECT 1", depends_on=["v1"], metadata=m)
        assert_raises_message(
            ManagedViewError,
            "dependency cycle detected among managed views",
            self._produce,
            m,
        )

    def test_self_dependency_cycle_error(self):
        m = MetaData()
        ManagedView("v1", "SELECT 1", depends_on=["v1"], metadata=m)
        assert_raises_message(
            ManagedViewError,
            "dependency cycle detected among managed views",
            self._produce,
            m,
        )

    def test_unresolvable_dependency_error(self):
        m = MetaData()
        ManagedView("v1", "SELECT 1", depends_on=["nope"], metadata=m)
        assert_raises_message(
            ManagedViewError,
            "managed view 'v1' depends on 'nope', which is not a "
            "declared managed view, a table in the target metadata, or "
            "an existing database object",
            self._produce,
            m,
        )

    def test_dependency_on_unmanaged_existing_object_ok(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE t1 (id INTEGER)"))
            conn.execute(text("CREATE VIEW ext AS SELECT id FROM t1"))
        m = MetaData()
        Table("t1", m, Column("id", Integer))
        ManagedView("v1", "SELECT id FROM ext", depends_on=["ext"],
                    metadata=m)
        ms = self._produce(m)
        flat = list(_flatten(ms.upgrade_ops.ops))
        eq_(
            [type(o).__name__ for o in flat],
            ["CreateViewOp"],
        )

    def test_second_autogen_after_apply_no_diffs(self):
        ms = self._produce(self._metadata())
        self._apply(ms.upgrade_ops.ops)
        mc = MigrationContext.configure(self.engine.connect())
        eq_(compare_metadata(mc, self._metadata()), [])

    def test_quoted_view_name_roundtrip(self):
        m = MetaData()
        t = Table("t1", m, Column("id", Integer))
        ManagedView(
            quoted_name("odd view", True),
            "SELECT id FROM t1",
            depends_on=[t],
            metadata=m,
        )
        ms = self._produce(m)
        self._apply(ms.upgrade_ops.ops)
        eq_(inspect(self.engine).get_view_names(), ["odd view"])
        mc = MigrationContext.configure(self.engine.connect())
        eq_(compare_metadata(mc, m), [])


_ENV_TEMPLATE = """
from sqlalchemy import MetaData, Table, Column, Integer, engine_from_config
from sqlalchemy.sql.elements import quoted_name
from alembic.views import ManagedView

target_metadata = MetaData()
t1 = Table(
    "t1",
    target_metadata,
    Column("id", Integer),
    Column("name", Integer),
%(table_extra)s
)
v1 = ManagedView("v1", %(v1_def)r, depends_on=[t1],
                 metadata=target_metadata)
v2 = ManagedView(
    quoted_name("odd view", True),
    "SELECT id FROM v1",
    depends_on=[v1],
    metadata=target_metadata,
)

engine = engine_from_config(
    config.get_section(config.config_ini_section),
    prefix="sqlalchemy.")
connection = engine.connect()

context.configure(
    connection=connection, target_metadata=target_metadata,
)

try:
    with context.begin_transaction():
        context.run_migrations()
finally:
    connection.close()
    engine.dispose()
"""


class ViewEndToEndTest(TestBase):
    """Run a full table -> v1 -> v2 dependency chain through generated
    migrations on SQLite, exercising upgrade, downgrade, and a second
    autogenerate pass."""

    __only_on__ = "sqlite"

    def setUp(self):
        self.env = staging_env()
        self.cfg = _sqlite_testing_config()

    def tearDown(self):
        clear_staging_env()

    def _env_fixture(self, v1_def, table_extra=""):
        env_file_fixture(
            _ENV_TEMPLATE
            % {"v1_def": v1_def, "table_extra": table_extra}
        )

    def _query(self, sql):
        db = _sqlite_file_db()
        with db.connect() as conn:
            return conn.execute(text(sql)).fetchall()

    def _view_definitions(self):
        rows = self._query(
            "SELECT name, sql FROM sqlite_master WHERE type='view' "
            "ORDER BY name"
        )
        return {name: sql for name, sql in rows}

    def test_full_lifecycle(self):
        # an unmanaged view that alembic must never touch
        db = _sqlite_file_db()
        with db.begin() as conn:
            conn.execute(text("CREATE TABLE seed (id INTEGER)"))
            conn.execute(
                text("CREATE VIEW custom AS SELECT id FROM seed")
            )

        v1_def_v1 = "SELECT id, name FROM t1"
        v1_def_v2 = "SELECT id, name, id + 1 AS bumped FROM t1"

        # revision 1: create table + v1 + v2 chain
        self._env_fixture(v1_def_v1)
        command.revision(self.cfg, message="rev1", autogenerate=True)
        command.upgrade(self.cfg, "head")

        with db.begin() as conn:
            conn.execute(text("INSERT INTO t1 (id, name) VALUES (1, 10)"))
        eq_(self._query('SELECT * FROM "odd view"'), [(1,)])

        # second autogenerate pass: no differences
        command.check(self.cfg)

        # revision 2: change the v1 definition
        self._env_fixture(v1_def_v2)
        command.revision(self.cfg, message="rev2", autogenerate=True)
        command.upgrade(self.cfg, "head")

        eq_(self._query('SELECT * FROM "odd view"'), [(1,)])
        defs = self._view_definitions()
        assert "bumped" in defs["v1"], defs
        assert "custom" in defs, "unmanaged view must survive"

        # no differences after the second upgrade either
        command.check(self.cfg)

        # downgrade one step: the pre-upgrade v1 definition is restored
        command.downgrade(self.cfg, "-1")
        eq_(self._query('SELECT * FROM "odd view"'), [(1,)])
        defs = self._view_definitions()
        assert "bumped" not in defs["v1"], defs
        assert "custom" in defs, "unmanaged view must survive"

        # downgrade to base: managed views and table are gone,
        # unmanaged objects remain
        command.downgrade(self.cfg, "base")
        defs = self._view_definitions()
        eq_(set(defs), {"custom"})
        eq_(
            self._query(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='t1'"
            ),
            [],
        )

"""Tests for managed view support: declaration, operations, autogenerate."""

from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import text
from sqlalchemy.sql.schema import quoted_name

from alembic import op
from alembic.autogenerate import produce_migrations
from alembic.autogenerate.api import render_python_code
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations import ops
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing.fixtures import op_fixture
from alembic.testing import TestBase
from alembic.util import CommandError
from alembic.views import normalize_view_definition
from alembic.views import register_views
from alembic.views import View


def _produce_ops(metadata, connection):
    migration_context = MigrationContext.configure(connection)
    return produce_migrations(migration_context, metadata)


def _apply_ops(operations, op_container):
    for op_ in op_container.ops:
        if hasattr(op_, "ops"):
            _apply_ops(operations, op_)
        else:
            operations.invoke(op_)


def _view_op_names(op_container):
    result = []
    for op_ in op_container.ops:
        if isinstance(op_, (ops.CreateViewOp, ops.DropViewOp)):
            result.append((type(op_).__name__, op_.view_name))
        elif hasattr(op_, "ops"):
            result.extend(_view_op_names(op_))
        else:
            result.append((type(op_).__name__, None))
    return result


class NormalizeViewDefinitionTest(TestBase):
    def test_create_view_prefix_and_semicolons(self):
        eq_(
            normalize_view_definition(
                "CREATE VIEW v1 AS SELECT id FROM t1;;"
            ),
            "SELECT id FROM t1",
        )

    def test_create_view_prefix_quoted_and_schema(self):
        eq_(
            normalize_view_definition(
                'CREATE VIEW "my schema"."my view" AS SELECT id FROM t1'
            ),
            "SELECT id FROM t1",
        )

    def test_whitespace_collapsed_outside_literals(self):
        eq_(
            normalize_view_definition("  SELECT   id,\n\tname  FROM   t1 "),
            "SELECT id, name FROM t1",
        )

    def test_string_literals_are_preserved(self):
        eq_(
            normalize_view_definition("SELECT 'a  b'   FROM t1"),
            "SELECT 'a  b' FROM t1",
        )
        eq_(
            normalize_view_definition("SELECT 'a''  b' FROM t1"),
            "SELECT 'a''  b' FROM t1",
        )

    def test_string_literal_differences_are_significant(self):
        assert normalize_view_definition(
            "SELECT 'a b' FROM t1"
        ) != normalize_view_definition("SELECT 'a  b' FROM t1")

    def test_quoted_identifiers_are_preserved(self):
        assert normalize_view_definition(
            'SELECT "a  b" FROM t1'
        ) != normalize_view_definition('SELECT "a b" FROM t1')


class ViewOperationsTest(TestBase):
    def test_create_view_offline(self):
        context = op_fixture("sqlite", as_sql=True)
        op.create_view("v1", "SELECT id FROM t1")
        context.assert_("CREATE VIEW v1 AS SELECT id FROM t1")

    def test_drop_view_offline(self):
        context = op_fixture("sqlite", as_sql=True)
        op.drop_view("v1")
        context.assert_("DROP VIEW v1")

    def test_create_view_offline_quoted_name(self):
        context = op_fixture("sqlite", as_sql=True)
        op.create_view(quoted_name("my view", quote=True), "SELECT 1")
        context.assert_('CREATE VIEW "my view" AS SELECT 1')

    def test_drop_view_offline_quoted_name(self):
        context = op_fixture("sqlite", as_sql=True)
        op.drop_view(quoted_name("my view", quote=True))
        context.assert_('DROP VIEW "my view"')

    def test_create_and_drop_view_online(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(text("CREATE TABLE t1 (id INTEGER)"))
            migration_context = MigrationContext.configure(connection)
            operations = Operations(migration_context)
            operations.create_view("v1", "SELECT id FROM t1")
            eq_(inspect(connection).get_view_names(), ["v1"])
            operations.drop_view("v1")
            eq_(inspect(connection).get_view_names(), [])

    def test_create_view_op_reverse(self):
        create_op = ops.CreateViewOp("v1", "SELECT id FROM t1")
        drop_op = create_op.reverse()
        eq_(drop_op.view_name, "v1")
        eq_(drop_op.reverse().definition, "SELECT id FROM t1")

    def test_drop_view_op_reverse_needs_definition(self):
        drop_op = ops.DropViewOp("v1")
        assert_raises_message(
            NotImplementedError,
            "reverse of drop_view is not implemented",
            drop_op.reverse,
        )

    def test_render_create_view(self):
        up_ops = ops.UpgradeOps(
            [ops.CreateViewOp("v1", "SELECT id FROM t1")]
        )
        eq_(
            render_python_code(up_ops),
            "# ### commands auto generated by Alembic - please adjust! ###\n"
            "    op.create_view('v1', 'SELECT id FROM t1')\n"
            "    # ### end Alembic commands ###",
        )

    def test_render_drop_view(self):
        up_ops = ops.UpgradeOps([ops.DropViewOp("v1")])
        eq_(
            render_python_code(up_ops),
            "# ### commands auto generated by Alembic - please adjust! ###\n"
            "    op.drop_view('v1')\n"
            "    # ### end Alembic commands ###",
        )

    def test_render_view_ops_with_schema(self):
        up_ops = ops.UpgradeOps(
            [
                ops.CreateViewOp("v1", "SELECT id FROM t1", schema="s1"),
                ops.DropViewOp("v2", schema="s1"),
            ]
        )
        eq_(
            render_python_code(up_ops),
            "# ### commands auto generated by Alembic - please adjust! ###\n"
            "    op.create_view('v1', 'SELECT id FROM t1', schema='s1')\n"
            "    op.drop_view('v2', schema='s1')\n"
            "    # ### end Alembic commands ###",
        )

class AutogenViewsTest(TestBase):
    def _table_metadata(self, extra_column=False):
        metadata = MetaData()
        columns = [
            Column("id", Integer),
            Column("name", String(50)),
        ]
        if extra_column:
            columns.append(Column("extra", Integer))
        Table("t1", metadata, *columns)
        return metadata

    def test_no_views_declared_no_ops(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(text("CREATE TABLE t1 (id INTEGER)"))
            metadata = MetaData()
            Table("t1", metadata, Column("id", Integer))
            migration_script = _produce_ops(metadata, connection)
            eq_(migration_script.upgrade_ops.as_diffs(), [])

    def test_no_diff_when_definitions_match(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            connection.execute(text("CREATE VIEW v1 AS SELECT id FROM t1"))
            connection.execute(text("CREATE VIEW v2 AS SELECT id FROM v1"))
            metadata = self._table_metadata()
            View("v1", "SELECT id FROM t1", depends_on=["t1"],
                 metadata=metadata)
            View("v2", "SELECT id FROM v1", depends_on=["v1"],
                 metadata=metadata)
            migration_script = _produce_ops(metadata, connection)
            eq_(migration_script.upgrade_ops.as_diffs(), [])

    def test_no_diff_whitespace_and_create_view_prefix(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            connection.execute(text("CREATE VIEW v1 AS SELECT id FROM t1"))
            metadata = self._table_metadata()
            View(
                "v1",
                "CREATE VIEW v1 AS\n   SELECT   id  FROM   t1 ;",
                depends_on=["t1"],
                metadata=metadata,
            )
            migration_script = _produce_ops(metadata, connection)
            eq_(migration_script.upgrade_ops.as_diffs(), [])

    def test_add_views_in_dependency_order(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            metadata = self._table_metadata()
            # declare v2 before v1; creation must still be ordered
            # v1 then v2 based on the dependency, not on names or
            # declaration order
            View("v2", "SELECT id FROM v1", depends_on=["v1"],
                 metadata=metadata)
            View("v1", "SELECT id FROM t1", depends_on=["t1"],
                 metadata=metadata)
            migration_script = _produce_ops(metadata, connection)
            eq_(
                _view_op_names(migration_script.upgrade_ops),
                [("CreateViewOp", "v1"), ("CreateViewOp", "v2")],
            )

    def test_changed_definition_rebuilt(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            connection.execute(text("CREATE VIEW v1 AS SELECT id FROM t1"))
            metadata = self._table_metadata()
            View("v1", "SELECT id, name FROM t1", depends_on=["t1"],
                 metadata=metadata)
            migration_script = _produce_ops(metadata, connection)
            eq_(
                _view_op_names(migration_script.upgrade_ops),
                [("DropViewOp", "v1"), ("CreateViewOp", "v1")],
            )

    def test_changed_definition_keeps_old_definition_for_downgrade(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            connection.execute(text("CREATE VIEW v1 AS SELECT id FROM t1"))
            metadata = self._table_metadata()
            View("v1", "SELECT id, name FROM t1", depends_on=["t1"],
                 metadata=metadata)
            migration_script = _produce_ops(metadata, connection)
            eq_(
                render_python_code(migration_script.upgrade_ops),
                "# ### commands auto generated by Alembic - please "
                "adjust! ###\n"
                "    op.drop_view('v1')\n"
                "    op.create_view('v1', 'SELECT id, name FROM t1')\n"
                "    # ### end Alembic commands ###",
            )
            eq_(
                render_python_code(migration_script.downgrade_ops),
                "# ### commands auto generated by Alembic - please "
                "adjust! ###\n"
                "    op.drop_view('v1')\n"
                "    op.create_view('v1', 'SELECT id FROM t1')\n"
                "    # ### end Alembic commands ###",
            )

    def test_dependents_rebuilt_when_view_changes(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            connection.execute(text("CREATE VIEW v1 AS SELECT id FROM t1"))
            connection.execute(text("CREATE VIEW v2 AS SELECT id FROM v1"))
            metadata = self._table_metadata()
            View("v1", "SELECT id, name FROM t1", depends_on=["t1"],
                 metadata=metadata)
            View("v2", "SELECT id FROM v1", depends_on=["v1"],
                 metadata=metadata)
            migration_script = _produce_ops(metadata, connection)
            eq_(
                _view_op_names(migration_script.upgrade_ops),
                [
                    ("DropViewOp", "v2"),
                    ("DropViewOp", "v1"),
                    ("CreateViewOp", "v1"),
                    ("CreateViewOp", "v2"),
                ],
            )

    def test_dependents_rebuilt_around_table_change(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            connection.execute(text("CREATE VIEW v1 AS SELECT id FROM t1"))
            connection.execute(text("CREATE VIEW v2 AS SELECT id FROM v1"))
            metadata = self._table_metadata(extra_column=True)
            View("v1", "SELECT id FROM t1", depends_on=["t1"],
                 metadata=metadata)
            View("v2", "SELECT id FROM v1", depends_on=["v1"],
                 metadata=metadata)
            migration_script = _produce_ops(metadata, connection)
            op_kinds = [
                type(op_).__name__
                for op_ in migration_script.upgrade_ops.ops
            ]
            eq_(
                op_kinds,
                [
                    "DropViewOp",
                    "DropViewOp",
                    "ModifyTableOps",
                    "CreateViewOp",
                    "CreateViewOp",
                ],
            )
            eq_(
                _view_op_names(migration_script.upgrade_ops),
                [
                    ("DropViewOp", "v2"),
                    ("DropViewOp", "v1"),
                    ("AddColumnOp", None),
                    ("CreateViewOp", "v1"),
                    ("CreateViewOp", "v2"),
                ],
            )

    def test_unmanaged_views_are_not_dropped(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            connection.execute(text("CREATE VIEW v1 AS SELECT id FROM t1"))
            connection.execute(
                text("CREATE VIEW not_managed AS SELECT name FROM t1")
            )
            metadata = self._table_metadata()
            View("v1", "SELECT id FROM t1", depends_on=["t1"],
                 metadata=metadata)
            migration_script = _produce_ops(metadata, connection)
            eq_(migration_script.upgrade_ops.as_diffs(), [])

    def test_dependency_cycle_is_an_error(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            metadata = self._table_metadata()
            View("v1", "SELECT id FROM v2", depends_on=["v2"],
                 metadata=metadata)
            View("v2", "SELECT id FROM v1", depends_on=["v1"],
                 metadata=metadata)
            assert_raises_message(
                CommandError,
                "Dependency cycle detected among managed views",
                _produce_ops,
                metadata,
                connection,
            )

    def test_unresolvable_dependency_is_an_error(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            metadata = self._table_metadata()
            View("v1", "SELECT id FROM t1", depends_on=["no_such_thing"],
                 metadata=metadata)
            assert_raises_message(
                CommandError,
                "Cannot resolve dependency 'no_such_thing' of managed "
                "view 'v1'",
                _produce_ops,
                metadata,
                connection,
            )

    def test_dependency_may_reference_unmanaged_database_view(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            connection.execute(
                text("CREATE VIEW helper AS SELECT id FROM t1")
            )
            metadata = self._table_metadata()
            View("v1", "SELECT id FROM helper", depends_on=["helper"],
                 metadata=metadata)
            migration_script = _produce_ops(metadata, connection)
            eq_(
                _view_op_names(migration_script.upgrade_ops),
                [("CreateViewOp", "v1")],
            )

    def test_depends_on_accepts_view_and_table_objects(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            metadata = self._table_metadata()
            t1 = metadata.tables["t1"]
            v1 = View("v1", "SELECT id FROM t1", depends_on=[t1])
            View("v2", "SELECT id FROM v1", depends_on=[v1],
                 metadata=metadata)
            View("v1b", "SELECT id FROM t1", depends_on=["t1"],
                 metadata=metadata)
            # v1 registered explicitly afterwards
            register_views(metadata, v1)
            migration_script = _produce_ops(metadata, connection)
            eq_(
                _view_op_names(migration_script.upgrade_ops),
                [
                    ("CreateViewOp", "v1b"),
                    ("CreateViewOp", "v1"),
                    ("CreateViewOp", "v2"),
                ],
            )

    def test_depends_on_undeclared_view_object_is_an_error(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            metadata = self._table_metadata()
            other = View("other", "SELECT id FROM t1")
            View("v1", "SELECT id FROM other", depends_on=[other],
                 metadata=metadata)
            assert_raises_message(
                CommandError,
                "Managed view 'v1' depends on view 'other', which is "
                "not declared as a managed view",
                _produce_ops,
                metadata,
                connection,
            )

    def test_invalid_new_definition_is_an_error(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            metadata = self._table_metadata()
            View("v1", "SELECT nope FROM t1", depends_on=["t1"],
                 metadata=metadata)
            assert_raises_message(
                CommandError,
                "The definition of managed view 'v1' is not valid",
                _produce_ops,
                metadata,
                connection,
            )

    def test_drop_table_with_dependent_view_is_an_error(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            connection.execute(text("CREATE VIEW v1 AS SELECT id FROM t1"))
            # t1 is removed from the metadata, so autogenerate would
            # drop it, but v1 still depends on it
            metadata = MetaData()
            View("v1", "SELECT id FROM t1", depends_on=["t1"],
                 metadata=metadata)
            assert_raises_message(
                CommandError,
                r"Managed view 'v1' depends on table\(s\) t1, which are "
                "being dropped",
                _produce_ops,
                metadata,
                connection,
            )

    def test_depends_on_single_table_object_not_iterated(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            metadata = self._table_metadata()
            View("v1", "SELECT id FROM t1", depends_on=metadata.tables["t1"],
                 metadata=metadata)
            migration_script = _produce_ops(metadata, connection)
            eq_(
                _view_op_names(migration_script.upgrade_ops),
                [("CreateViewOp", "v1")],
            )

    def test_index_only_table_change_does_not_rebuild_views(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            connection.execute(text("CREATE VIEW v1 AS SELECT id FROM t1"))
            # target metadata only adds an index; the view is unchanged
            # and must not be rebuilt
            metadata = MetaData()
            Table(
                "t1",
                metadata,
                Column("id", Integer),
                Column("name", String(50), index=True),
            )
            View("v1", "SELECT id FROM t1", depends_on=["t1"],
                 metadata=metadata)
            migration_script = _produce_ops(metadata, connection)
            eq_(
                _view_op_names(migration_script.upgrade_ops),
                [("CreateIndexOp", None)],
            )

    def test_quoted_view_name_roundtrip(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            connection.execute(
                text('CREATE VIEW "my view" AS SELECT id FROM t1')
            )
            metadata = self._table_metadata()
            View(
                quoted_name("my view", True),
                "SELECT id FROM t1",
                depends_on=["t1"],
                metadata=metadata,
            )
            migration_script = _produce_ops(metadata, connection)
            eq_(migration_script.upgrade_ops.as_diffs(), [])

    def test_quoted_view_name_rebuilt(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            connection.execute(
                text('CREATE VIEW "my view" AS SELECT id FROM t1')
            )
            metadata = self._table_metadata()
            View(
                quoted_name("my view", True),
                "SELECT id, name FROM t1",
                depends_on=["t1"],
                metadata=metadata,
            )
            migration_script = _produce_ops(metadata, connection)
            eq_(
                _view_op_names(migration_script.upgrade_ops),
                [("DropViewOp", "my view"), ("CreateViewOp", "my view")],
            )


class SQLiteEndToEndTest(TestBase):
    """Build a table -> V1 -> V2 dependency chain on SQLite, generate a
    migration, run upgrade and downgrade, querying V2 in each state.

    """

    def test_full_migration_roundtrip(self):
        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(
                text("CREATE TABLE t1 (id INTEGER, name VARCHAR(50))")
            )
            connection.execute(
                text("INSERT INTO t1 VALUES (1, 'x'), (2, 'y')")
            )
            connection.execute(text("CREATE VIEW v1 AS SELECT id FROM t1"))
            connection.execute(text("CREATE VIEW v2 AS SELECT id FROM v1"))

            eq_(
                connection.execute(text("SELECT * FROM v2")).fetchall(),
                [(1,), (2,)],
            )

            # new model: t1 gains a column, v1 projects it, v2 is
            # unchanged but depends on v1
            metadata = MetaData()
            Table(
                "t1",
                metadata,
                Column("id", Integer),
                Column("name", String(50)),
                Column("extra", Integer),
            )
            View("v1", "SELECT id, extra FROM t1", depends_on=["t1"],
                 metadata=metadata)
            View("v2", "SELECT id FROM v1", depends_on=["v1"],
                 metadata=metadata)

            migration_context = MigrationContext.configure(connection)
            operations = Operations(migration_context)
            migration_script = _produce_ops(metadata, connection)

            _apply_ops(operations, migration_script.upgrade_ops)
            eq_(
                connection.execute(text("SELECT * FROM v2")).fetchall(),
                [(1,), (2,)],
            )
            eq_(
                inspect(connection).get_view_definition("v1"),
                "CREATE VIEW v1 AS SELECT id, extra FROM t1",
            )

            # a second autogenerate pass produces no operations
            migration_script_two = _produce_ops(metadata, connection)
            eq_(migration_script_two.upgrade_ops.as_diffs(), [])

            # downgrade restores the previous definitions
            _apply_ops(operations, migration_script.downgrade_ops)
            eq_(
                connection.execute(text("SELECT * FROM v2")).fetchall(),
                [(1,), (2,)],
            )
            eq_(
                inspect(connection).get_view_definition("v1"),
                "CREATE VIEW v1 AS SELECT id FROM t1",
            )

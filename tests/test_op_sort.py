"""Tests for the optional dependency-aware operation sorting phase."""

from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import ForeignKey
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import Table
from sqlalchemy import text
from sqlalchemy.engine.default import DefaultDialect

import pytest

from alembic.autogenerate import api
from alembic.autogenerate import render_python_code
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations import ops
from alembic.operations.sort import ObjectRef
from alembic.operations.sort import OpSortCycleError
from alembic.operations.sort import sort_migration_script
from alembic.operations.sort import sort_ops
from alembic.testing import eq_
from alembic.testing import TestBase


def _plain_table(name, schema=None):
    return Table(
        name,
        MetaData(),
        Column("id", Integer, primary_key=True),
        schema=schema,
    )


def _parent_child_tables():
    metadata = MetaData()
    parent = Table(
        "parent", metadata, Column("id", Integer, primary_key=True)
    )
    child = Table(
        "child",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("parent.id")),
    )
    return parent, child


def _mutual_tables():
    metadata = MetaData()
    table_a = Table(
        "a",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("b_id", Integer),
        ForeignKeyConstraint(["b_id"], ["b.id"], name="fk_a_b"),
    )
    table_b = Table(
        "b",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("a_id", Integer),
        ForeignKeyConstraint(["a_id"], ["a.id"], name="fk_b_a"),
    )
    return table_a, table_b


def _cross_schema_tables():
    metadata = MetaData()
    parent = Table(
        "parent",
        metadata,
        Column("id", Integer, primary_key=True),
        schema="s1",
    )
    child = Table(
        "child",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer),
        ForeignKeyConstraint(
            ["parent_id"], ["s1.parent.id"], name="fk_child_parent"
        ),
        schema="s2",
    )
    return parent, child


def split_out_foreign_keys(create_table_op):
    """Test-local plugin: split FK constraints out of a CreateTableOp.

    The foreign keys are returned as explicit post-operations so that
    mutually dependent tables no longer form a dependency cycle.
    """
    fk_constraints = [
        item
        for item in create_table_op.columns
        if isinstance(item, ForeignKeyConstraint)
    ]
    if not fk_constraints:
        return [create_table_op]
    create_table_op.columns = [
        item
        for item in create_table_op.columns
        if not isinstance(item, ForeignKeyConstraint)
    ]
    return [create_table_op] + [
        ops.CreateForeignKeyOp.from_constraint(constraint)
        for constraint in fk_constraints
    ]


class CustomNoDepsOp(ops.MigrateOperation):
    """A custom operation that declares no dependencies."""

    def reverse(self):
        return self


class OpDependencyDeclarationTest(TestBase):
    def test_create_table_declares_fk_reads(self):
        _, child = _parent_child_tables()
        deps = ops.CreateTableOp.from_table(child).op_dependencies()
        eq_(deps.creates, {ObjectRef.table("child")})
        eq_(deps.reads, {ObjectRef.table("parent")})

    def test_cross_schema_fk_read(self):
        _, child = _cross_schema_tables()
        deps = ops.CreateTableOp.from_table(child).op_dependencies()
        eq_(deps.reads, {ObjectRef.table("parent", "s1")})

    def test_rename_declares_both_identities(self):
        deps = ops.RenameTableOp("old", "new", schema="s").op_dependencies()
        eq_(
            deps.renames,
            ((ObjectRef.table("old", "s"), ObjectRef.table("new", "s")),),
        )

    def test_unknown_ops_declare_nothing(self):
        eq_(ops.ExecuteSQLOp("select 1").op_dependencies(), None)
        eq_(CustomNoDepsOp().op_dependencies(), None)


class OpSortTest(TestBase):
    def test_create_table_before_dependent_fk(self):
        parent, child = _parent_child_tables()
        create_parent = ops.CreateTableOp.from_table(parent)
        create_child = ops.CreateTableOp.from_table(child)
        fk_op = ops.CreateForeignKeyOp(
            "fk_child_parent", "child", "parent", ["parent_id"], ["id"]
        )
        upgrade_ops = ops.UpgradeOps([fk_op, create_child, create_parent])

        sort_ops(upgrade_ops)

        eq_(upgrade_ops.ops, [create_parent, create_child, fk_op])

    def test_unrelated_ops_keep_original_order(self):
        create_ops = [
            ops.CreateTableOp.from_table(_plain_table(name))
            for name in ("t3", "t1", "t2")
        ]
        upgrade_ops = ops.UpgradeOps(list(create_ops))

        sort_ops(upgrade_ops)

        eq_(upgrade_ops.ops, create_ops)

    def test_custom_op_is_ordering_barrier(self):
        parent, child = _parent_child_tables()
        create_parent = ops.CreateTableOp.from_table(parent)
        create_child = ops.CreateTableOp.from_table(child)
        fk_op = ops.CreateForeignKeyOp(
            "fk_child_parent", "child", "parent", ["parent_id"], ["id"]
        )
        barrier = CustomNoDepsOp()
        upgrade_ops = ops.UpgradeOps(
            [fk_op, create_parent, barrier, create_child]
        )

        sort_ops(upgrade_ops)

        # fk_op may move past create_parent, but neither it nor
        # create_parent may cross the barrier, and create_child stays
        # on its own side.
        eq_(upgrade_ops.ops, [create_parent, fk_op, barrier, create_child])

    def test_execute_sql_is_ordering_barrier(self):
        parent, child = _parent_child_tables()
        create_parent = ops.CreateTableOp.from_table(parent)
        create_child = ops.CreateTableOp.from_table(child)
        barrier = ops.ExecuteSQLOp("SELECT 1")
        upgrade_ops = ops.UpgradeOps([create_child, barrier, create_parent])

        sort_ops(upgrade_ops)

        # even though child refers to parent, nothing may cross the
        # unknown ExecuteSQLOp
        eq_(upgrade_ops.ops, [create_child, barrier, create_parent])

    def test_rename_orders_old_and_new_identities(self):
        create_old = ops.CreateTableOp.from_table(_plain_table("old_name"))
        rename = ops.RenameTableOp("old_name", "new_name")
        index = ops.CreateIndexOp("ix_new", "new_name", ["id"])
        upgrade_ops = ops.UpgradeOps([index, rename, create_old])

        sort_ops(upgrade_ops)

        eq_(upgrade_ops.ops, [create_old, rename, index])

    def test_cycle_error_names_objects_and_ops(self):
        table_a, table_b = _mutual_tables()
        upgrade_ops = ops.UpgradeOps(
            [
                ops.CreateTableOp.from_table(table_a),
                ops.CreateTableOp.from_table(table_b),
            ]
        )

        with pytest.raises(OpSortCycleError) as excinfo:
            sort_ops(upgrade_ops)

        message = str(excinfo.value)
        assert "dependency cycle" in message
        assert 'table "a"' in message
        assert 'table "b"' in message
        assert "CreateTableOp('a')" in message
        assert "CreateTableOp('b')" in message
        assert "must precede" in message

    def test_plugin_split_post_ops_resolve_cycle(self):
        table_a, table_b = _mutual_tables()
        split = split_out_foreign_keys(
            ops.CreateTableOp.from_table(table_a)
        ) + split_out_foreign_keys(ops.CreateTableOp.from_table(table_b))
        creates = [
            op for op in split if isinstance(op, ops.CreateTableOp)
        ]
        fks = [op for op in split if isinstance(op, ops.CreateForeignKeyOp)]
        eq_(len(creates), 2)
        eq_(len(fks), 2)

        # scrambled: foreign keys first
        upgrade_ops = ops.UpgradeOps(fks + creates)
        sort_ops(upgrade_ops)

        eq_(upgrade_ops.ops, creates + fks)

    def test_modify_table_ops_boundary_preserved(self):
        parent, _ = _parent_child_tables()
        fk_op = ops.CreateForeignKeyOp(
            "fk_child_parent", "child", "parent", ["parent_id"], ["id"]
        )
        modify = ops.ModifyTableOps("child", [fk_op])
        create_parent = ops.CreateTableOp.from_table(parent)
        upgrade_ops = ops.UpgradeOps([modify, create_parent])

        sort_ops(upgrade_ops)

        # the container moves as a whole; its contents stay inside
        eq_(upgrade_ops.ops, [create_parent, modify])
        eq_(modify.ops, [fk_op])

    def test_container_contents_sorted_recursively(self):
        add_column = ops.AddColumnOp("t", Column("x", Integer))
        drop_column = ops.DropColumnOp("t", "x")
        modify = ops.ModifyTableOps("t", [add_column, drop_column])
        upgrade_ops = ops.UpgradeOps([modify])

        sort_ops(upgrade_ops)

        # drop precedes re-create of the same column
        eq_(modify.ops, [drop_column, add_column])

    def test_cross_schema_create_order(self):
        parent, child = _cross_schema_tables()
        create_parent = ops.CreateTableOp.from_table(parent)
        create_child = ops.CreateTableOp.from_table(child)
        upgrade_ops = ops.UpgradeOps([create_child, create_parent])

        sort_ops(upgrade_ops)

        eq_(upgrade_ops.ops, [create_parent, create_child])

    def test_downgrade_drops_child_before_parent(self):
        parent, child = _parent_child_tables()
        drop_parent = ops.DropTableOp.from_table(parent)
        drop_child = ops.DropTableOp.from_table(child)
        downgrade_ops = ops.DowngradeOps([drop_parent, drop_child])

        sort_ops(downgrade_ops)

        eq_(downgrade_ops.ops, [drop_child, drop_parent])

    def test_sort_migration_script_sorts_both_directions(self):
        parent, child = _parent_child_tables()
        create_parent = ops.CreateTableOp.from_table(parent)
        create_child = ops.CreateTableOp.from_table(child)
        fk_op = ops.CreateForeignKeyOp(
            "fk_child_parent", "child", "parent", ["parent_id"], ["id"]
        )
        drop_parent = ops.DropTableOp.from_table(parent)
        drop_child = ops.DropTableOp.from_table(child)
        script = ops.MigrationScript(
            rev_id="x",
            upgrade_ops=ops.UpgradeOps([fk_op, create_child, create_parent]),
            downgrade_ops=ops.DowngradeOps([drop_parent, drop_child]),
        )

        sort_migration_script(script)

        eq_(script.upgrade_ops.ops, [create_parent, create_child, fk_op])
        eq_(script.downgrade_ops.ops, [drop_child, drop_parent])


class RevisionContextSortPhaseTest(TestBase):
    """The optional sorting phase wired into autogenerate revision
    production."""

    def _revision_context(self, sort_ops_enabled, inject):
        command_args = {
            "rev_id": "abc",
            "message": "test",
            "head": None,
            "splice": False,
            "branch_label": None,
            "version_path": None,
            "depends_on": None,
            "sql": False,
        }
        revision_context = api.RevisionContext(
            None, None, command_args, process_revision_directives=inject
        )
        migration_context = MigrationContext.configure(
            dialect=DefaultDialect(),
            opts={
                "upgrade_token": "upgrades",
                "downgrade_token": "downgrades",
                "process_revision_directives": None,
                "sort_ops": sort_ops_enabled,
            },
        )
        revision_context._run_environment("abc", migration_context, False)
        return revision_context

    def _scrambled_ops(self):
        parent, child = _parent_child_tables()
        create_parent = ops.CreateTableOp.from_table(parent)
        create_child = ops.CreateTableOp.from_table(child)
        fk_op = ops.CreateForeignKeyOp(
            "fk_child_parent", "child", "parent", ["parent_id"], ["id"]
        )
        return [fk_op, create_child, create_parent]

    def test_sort_ops_phase_sorts_when_enabled(self):
        scrambled = self._scrambled_ops()

        def inject(context, revision, directives):
            directives[-1].upgrade_ops.ops[:] = scrambled

        revision_context = self._revision_context(True, inject)
        sorted_ops = (
            revision_context.generated_revisions[-1].upgrade_ops.ops
        )
        eq_(
            [type(op) for op in sorted_ops],
            [ops.CreateTableOp, ops.CreateTableOp, ops.CreateForeignKeyOp],
        )
        eq_(sorted_ops[0].table_name, "parent")

    def test_sort_ops_phase_off_by_default(self):
        scrambled = self._scrambled_ops()

        def inject(context, revision, directives):
            directives[-1].upgrade_ops.ops[:] = scrambled

        revision_context = self._revision_context(False, inject)
        eq_(
            revision_context.generated_revisions[-1].upgrade_ops.ops,
            scrambled,
        )


class SQLiteRoundTripTest(TestBase):
    """Sorted scripts are executed for real against SQLite.

    Foreign key add/drop operations need ALTER TABLE ... ADD/DROP
    CONSTRAINT, which SQLite only supports through batch mode; those
    operations are therefore verified through the rendered script text,
    while the SQLite-executable subset is invoked for real.
    """

    def _run_executable_subset(self, connection, op_list):
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        for op_ in op_list:
            if isinstance(op_, (ops.AddConstraintOp, ops.DropConstraintOp)):
                continue
            operations.invoke(op_)

    def test_mutual_fk_tables_with_barrier_round_trip(self):
        table_a, table_b = _mutual_tables()
        create_a, fk_a = split_out_foreign_keys(
            ops.CreateTableOp.from_table(table_a)
        )
        create_b, fk_b = split_out_foreign_keys(
            ops.CreateTableOp.from_table(table_b)
        )
        barrier_up = ops.ExecuteSQLOp(
            "CREATE TABLE run_log (id INTEGER NOT NULL)"
        )
        barrier_down = ops.ExecuteSQLOp("DROP TABLE run_log")
        upgrade_ops = ops.UpgradeOps(
            [fk_b, create_a, barrier_up, fk_a, create_b]
        )
        # the downgrade stream mirrors the reversed upgrade: foreign
        # keys are dropped as separate ops, and the table drops carry
        # no FK references since the FKs were split out
        drop_fk_a = ops.DropConstraintOp("fk_a_b", "a", type_="foreignkey")
        drop_fk_b = ops.DropConstraintOp("fk_b_a", "b", type_="foreignkey")
        drop_a = create_a.reverse()
        drop_b = create_b.reverse()
        downgrade_ops = ops.DowngradeOps(
            [barrier_down, drop_a, drop_fk_a, drop_b, drop_fk_b]
        )
        script = ops.MigrationScript(
            rev_id="x",
            upgrade_ops=upgrade_ops,
            downgrade_ops=downgrade_ops,
        )

        sort_migration_script(script)

        # structural order: regions sorted, barrier untouched
        eq_(
            upgrade_ops.ops,
            [create_a, fk_b, barrier_up, create_b, fk_a],
        )
        eq_(
            downgrade_ops.ops,
            [barrier_down, drop_fk_a, drop_a, drop_fk_b, drop_b],
        )

        # the rendered script has the same order
        rendered_up = render_python_code(upgrade_ops)
        assert (
            rendered_up.index("op.create_table('a'")
            < rendered_up.index("op.create_foreign_key('fk_b_a'")
            < rendered_up.index("CREATE TABLE run_log")
            < rendered_up.index("op.create_table('b'")
            < rendered_up.index("op.create_foreign_key('fk_a_b'")
        )
        rendered_down = render_python_code(downgrade_ops)
        assert rendered_down.index(
            "op.drop_constraint('fk_a_b'"
        ) < rendered_down.index("op.drop_table('a'")
        assert rendered_down.index(
            "op.drop_constraint('fk_b_a'"
        ) < rendered_down.index("op.drop_table('b'")

        # the SQLite-executable subset upgrades and downgrades for real
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            self._run_executable_subset(connection, upgrade_ops.ops)
            eq_(
                sorted(inspect(connection).get_table_names()),
                ["a", "b", "run_log"],
            )
            self._run_executable_subset(connection, downgrade_ops.ops)
            eq_(inspect(connection).get_table_names(), [])

    def test_cross_schema_round_trip(self):
        parent, child = _cross_schema_tables()
        create_child, fk_child = split_out_foreign_keys(
            ops.CreateTableOp.from_table(child)
        )
        create_parent = ops.CreateTableOp.from_table(parent)
        upgrade_ops = ops.UpgradeOps([create_child, fk_child, create_parent])
        downgrade_ops = ops.DowngradeOps(
            [
                ops.DropTableOp.from_table(parent),
                ops.DropTableOp.from_table(child),
            ]
        )
        script = ops.MigrationScript(
            rev_id="x",
            upgrade_ops=upgrade_ops,
            downgrade_ops=downgrade_ops,
        )

        sort_migration_script(script)

        # the two creates have no dependency between them once the
        # cross-schema FK is split out, so they keep their relative
        # order; the FK post-op is ordered after both
        eq_(upgrade_ops.ops, [create_child, create_parent, fk_child])
        eq_(
            [op.table_name for op in downgrade_ops.ops],
            ["child", "parent"],
        )

        rendered_up = render_python_code(upgrade_ops)
        assert (
            rendered_up.index("op.create_table('child'")
            < rendered_up.index("op.create_table('parent'")
            < rendered_up.index("op.create_foreign_key('fk_child_parent'")
        )

        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text("ATTACH DATABASE ':memory:' AS s1"))
            connection.execute(text("ATTACH DATABASE ':memory:' AS s2"))
            self._run_executable_subset(connection, upgrade_ops.ops)
            eq_(inspect(connection).get_table_names(schema="s1"), ["parent"])
            eq_(inspect(connection).get_table_names(schema="s2"), ["child"])
            self._run_executable_subset(connection, downgrade_ops.ops)
            eq_(inspect(connection).get_table_names(schema="s1"), [])
            eq_(inspect(connection).get_table_names(schema="s2"), [])

"""Tests for the optional dependency-aware operation ordering stage."""

import pytest
import sqlalchemy
from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import ForeignKey
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import Table
from sqlalchemy import text

from alembic import autogenerate
from alembic.autogenerate import sort_ops
from alembic.autogenerate.order import OpOrderingError
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations.ops import AddColumnOp
from alembic.operations.ops import CreateForeignKeyOp
from alembic.operations.ops import CreateIndexOp
from alembic.operations.ops import CreateTableOp
from alembic.operations.ops import DowngradeOps
from alembic.operations.ops import DropTableOp
from alembic.operations.ops import ExecuteSQLOp
from alembic.operations.ops import MigrateOperation
from alembic.operations.ops import ModifyTableOps
from alembic.operations.ops import ObjectRef
from alembic.operations.ops import OpDependency
from alembic.operations.ops import RenameTableOp
from alembic.operations.ops import UpgradeOps
from alembic.testing import eq_
from alembic.testing import TestBase


def _create_table(name, *extra, schema=None):
    return CreateTableOp(
        name,
        [Column("id", Integer, primary_key=True), *extra],
        schema=schema,
    )


class ReadTableOp(MigrateOperation):
    """Custom operation that declares which table it reads."""

    def __init__(self, table_name, schema=None):
        self.table_name = table_name
        self.schema = schema

    def get_dependencies(self):
        return OpDependency(
            reads={ObjectRef.table(self.table_name, self.schema)}
        )

    def reverse(self):
        return self


class OpaqueCustomOp(MigrateOperation):
    """Custom operation with no dependency declaration."""

    def reverse(self):
        return self


class CreateViewLikeOp(MigrateOperation):
    """Custom operation that creates one object while reading another."""

    def __init__(self, name, depends_on=None):
        self.table_name = name
        self.depends_on = depends_on

    def get_dependencies(self):
        reads = (
            {ObjectRef.table(self.depends_on)}
            if self.depends_on
            else set()
        )
        return OpDependency(
            creates={ObjectRef.table(self.table_name)}, reads=reads
        )

    def reverse(self):
        return self


class SortOpsTest(TestBase):
    """Unit tests for sort_ops() over hand-built operation lists."""

    def test_create_ordered_before_dependent(self):
        create_b = _create_table(
            "b", Column("a_id", Integer, ForeignKey("a.id"))
        )
        create_a = _create_table("a")
        upgrade_ops = UpgradeOps([create_b, create_a])
        sort_ops(upgrade_ops)
        eq_(upgrade_ops.ops, [create_a, create_b])

    def test_unrelated_ops_keep_original_order(self):
        ops = [_create_table("t3"), _create_table("t1"), _create_table("t2")]
        upgrade_ops = UpgradeOps(ops)
        sort_ops(upgrade_ops)
        eq_(upgrade_ops.ops, ops)

    def test_read_ordered_before_drop(self):
        read = ReadTableOp("a")
        drop = DropTableOp("a")
        upgrade_ops = UpgradeOps([drop, read])
        sort_ops(upgrade_ops)
        eq_(upgrade_ops.ops, [read, drop])

    def test_drop_ordered_before_recreate(self):
        create = _create_table("a")
        drop = DropTableOp("a")
        upgrade_ops = UpgradeOps([create, drop])
        sort_ops(upgrade_ops)
        # a create/drop pair on the same object is ambiguous
        # (temporary object vs. drop-and-recreate); the original
        # relative order is kept
        eq_(upgrade_ops.ops, [create, drop])

    def test_unknown_ops_act_as_barriers(self):
        create_b = _create_table(
            "b", Column("a_id", Integer, ForeignKey("a.id"))
        )
        create_a = _create_table("a")
        barrier_sql = ExecuteSQLOp("SELECT 1")
        barrier_custom = OpaqueCustomOp()
        upgrade_ops = UpgradeOps(
            [create_b, barrier_sql, barrier_custom, create_a]
        )
        sort_ops(upgrade_ops)
        # create_a may not be moved across the barriers, even though
        # create_b depends on it
        eq_(upgrade_ops.ops, [create_b, barrier_sql, barrier_custom, create_a])

    def test_custom_op_with_declared_dependencies(self):
        read_audit = ReadTableOp("audit_log")
        create_audit = _create_table("audit_log")
        upgrade_ops = UpgradeOps([read_audit, create_audit])
        sort_ops(upgrade_ops)
        eq_(upgrade_ops.ops, [create_audit, read_audit])

    def test_rename_before_and_after_identities(self):
        read_old = ReadTableOp("old_name")
        read_new = ReadTableOp("new_name")
        rename = RenameTableOp("old_name", "new_name")
        create_index = CreateIndexOp("ix_new", "new_name", ["id"])
        upgrade_ops = UpgradeOps([create_index, read_new, rename, read_old])
        sort_ops(upgrade_ops)
        ops = upgrade_ops.ops
        # the operation reading the old identity must run before the
        # rename; operations reading the new identity must run after
        assert ops.index(read_old) < ops.index(rename)
        assert ops.index(rename) < ops.index(read_new)
        assert ops.index(rename) < ops.index(create_index)

    def test_modify_table_ops_boundary_preserved(self):
        add_col = AddColumnOp(
            "b", Column("a_id", Integer, ForeignKey("a.id"))
        )
        modify = ModifyTableOps("b", [add_col])
        upgrade_ops = UpgradeOps(
            [modify, _create_table("a"), _create_table("b")]
        )
        sort_ops(upgrade_ops)
        ops = upgrade_ops.ops
        eq_(
            [type(op) for op in ops],
            [CreateTableOp, CreateTableOp, ModifyTableOps],
        )
        # the container moved as a unit; its contents were not
        # pulled out of the batch boundary
        eq_(ops[2].ops, [add_col])

        src = autogenerate.render_python_code(
            upgrade_ops, render_as_batch=True
        )
        assert src.index("create_table('b'") < src.index(
            "batch_alter_table('b'"
        )
        assert src.index("batch_alter_table('b'") < src.index("add_column")

    def test_cycle_reports_objects_and_ops(self):
        upgrade_ops = UpgradeOps(
            [
                _create_table(
                    "a", Column("b_id", Integer, ForeignKey("b.id"))
                ),
                _create_table(
                    "b", Column("a_id", Integer, ForeignKey("a.id"))
                ),
            ]
        )
        with pytest.raises(OpOrderingError) as exc:
            sort_ops(upgrade_ops)
        msg = str(exc.value)
        assert "dependency cycle" in msg
        assert "CreateTableOp('a')" in msg
        assert "CreateTableOp('b')" in msg
        assert "table 'a'" in msg
        assert "table 'b'" in msg

    def test_use_alter_fks_split_into_post_ops(self):
        upgrade_ops = UpgradeOps(
            [
                _create_table(
                    "b",
                    Column("a_id", Integer),
                    ForeignKeyConstraint(
                        ["a_id"], ["a.id"], name="fk_b_a", use_alter=True
                    ),
                ),
                _create_table(
                    "a",
                    Column("b_id", Integer),
                    ForeignKeyConstraint(
                        ["b_id"], ["b.id"], name="fk_a_b", use_alter=True
                    ),
                ),
            ]
        )
        sort_ops(upgrade_ops)
        ops = upgrade_ops.ops
        eq_(len(ops), 4)
        create_ops, fk_ops = ops[:2], ops[2:]
        for op in create_ops:
            assert isinstance(op, CreateTableOp)
            assert not any(
                isinstance(item, ForeignKeyConstraint) for item in op.columns
            )
        eq_(
            {(op.source_table, op.referent_table) for op in fk_ops},
            {("a", "b"), ("b", "a")},
        )
        for op in fk_ops:
            assert isinstance(op, CreateForeignKeyOp)


class _SQLiteSubset:
    """Operations wrapper for the SQLite-executable subset of a script.

    SQLite cannot ``ALTER TABLE ADD/DROP CONSTRAINT``, so
    create_foreign_key / drop_constraint form the non-executable
    subset and are skipped here.
    """

    def __init__(self, operations):
        self._operations = operations

    def __getattr__(self, name):
        return getattr(self._operations, name)

    def create_foreign_key(self, *arg, **kw):
        pass

    def drop_constraint(self, *arg, **kw):
        pass


class AutogenOrderingTest(TestBase):
    """Integration tests: autogenerate + ordering against SQLite."""

    def _produce(self, conn, metadata, **opts):
        opts.setdefault("autogenerate_order_ops", True)
        context = MigrationContext.configure(connection=conn, opts=opts)
        return autogenerate.produce_migrations(context, metadata)

    def test_extension_ops_cycle_raises(self):
        # an autogenerate extension appends custom operations that
        # declare dependencies; a cycle among them is reported with
        # the concrete objects and operations involved
        metadata = MetaData()
        Table("a", metadata, Column("id", Integer, primary_key=True))
        engine = create_engine("sqlite://")
        with engine.connect() as conn:
            migration_script = self._produce(conn, metadata)
        upgrade_ops = migration_script.upgrade_ops
        upgrade_ops.ops.extend(
            [
                CreateViewLikeOp("v1", depends_on="v2"),
                CreateViewLikeOp("v2", depends_on="v1"),
            ]
        )
        with pytest.raises(OpOrderingError) as exc:
            sort_ops(upgrade_ops)
        msg = str(exc.value)
        assert "dependency cycle" in msg
        assert "CreateViewLikeOp('v1')" in msg
        assert "CreateViewLikeOp('v2')" in msg
        assert "table 'v1'" in msg
        assert "table 'v2'" in msg

    def test_cross_schema_references(self):
        engine = create_engine("sqlite://")
        with engine.connect() as conn:
            conn.execute(text("ATTACH DATABASE ':memory:' AS other"))
            metadata = MetaData()
            Table(
                "child",
                metadata,
                Column("id", Integer, primary_key=True),
                Column("parent_id", Integer, ForeignKey("other.parent.id")),
            )
            Table(
                "parent",
                metadata,
                Column("id", Integer, primary_key=True),
                schema="other",
            )
            migration_script = self._produce(
                conn, metadata, include_schemas=True
            )

        upgrade_ops = migration_script.upgrade_ops
        downgrade_ops = migration_script.downgrade_ops

        # scramble the upgrade ops; the sorter must restore the
        # cross-schema dependency order
        upgrade_ops.ops.reverse()
        sort_ops(upgrade_ops)
        eq_(
            [(type(op).__name__, op.table_name) for op in upgrade_ops.ops],
            [("CreateTableOp", "parent"), ("CreateTableOp", "child")],
        )

        sort_ops(downgrade_ops)
        eq_(
            [(type(op).__name__, op.table_name) for op in downgrade_ops.ops],
            [("DropTableOp", "child"), ("DropTableOp", "parent")],
        )

    def test_barrier_blocks_reordering(self):
        metadata = MetaData()
        Table(
            "parent",
            metadata,
            Column("id", Integer, primary_key=True),
        )
        Table(
            "child",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("parent_id", Integer, ForeignKey("parent.id")),
        )
        engine = create_engine("sqlite://")
        with engine.connect() as conn:
            context = MigrationContext.configure(connection=conn, opts={})
            migration_script = autogenerate.produce_migrations(
                context, metadata
            )

        upgrade_ops = migration_script.upgrade_ops
        # scramble so that the dependent table is created first, with
        # an opaque operation in between
        upgrade_ops.ops.reverse()
        barrier = ExecuteSQLOp("SELECT 1")
        upgrade_ops.ops.insert(1, barrier)
        sort_ops(upgrade_ops)
        eq_(
            [type(op).__name__ for op in upgrade_ops.ops],
            ["CreateTableOp", "ExecuteSQLOp", "CreateTableOp"],
        )
        eq_(upgrade_ops.ops[0].table_name, "child")

        # without the barrier, the dependency order is restored
        upgrade_ops.ops.remove(barrier)
        sort_ops(upgrade_ops)
        eq_(
            [op.table_name for op in upgrade_ops.ops],
            ["parent", "child"],
        )

    def test_generated_script_upgrade_downgrade_sqlite(self):
        metadata = MetaData()
        Table(
            "a",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("b_id", Integer),
            ForeignKeyConstraint(
                ["b_id"], ["b.id"], name="fk_a_b", use_alter=True
            ),
        )
        Table(
            "b",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("a_id", Integer),
            ForeignKeyConstraint(
                ["a_id"], ["a.id"], name="fk_b_a", use_alter=True
            ),
        )
        Table(
            "c",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("a_id", Integer, ForeignKey("a.id")),
        )

        engine = create_engine("sqlite://")
        with engine.connect() as conn:
            migration_script = self._produce(conn, metadata)
            upgrade_ops = migration_script.upgrade_ops

            # an opaque operation is a barrier; placed first, the
            # remaining operations are still sorted behind it
            upgrade_ops.ops.insert(0, ExecuteSQLOp("SELECT 1"))
            sort_ops(upgrade_ops)
            assert isinstance(upgrade_ops.ops[0], ExecuteSQLOp)

            # build the downgrade from the non-barrier operations
            downgrade_ops = DowngradeOps([])
            UpgradeOps(
                [
                    op
                    for op in upgrade_ops.ops
                    if not isinstance(op, ExecuteSQLOp)
                ]
            ).reverse_into(downgrade_ops)
            sort_ops(downgrade_ops)

            context = MigrationContext.configure(connection=conn)
            upgrade_src = autogenerate.render_python_code(
                upgrade_ops, migration_context=context
            )
            downgrade_src = autogenerate.render_python_code(
                downgrade_ops, migration_context=context
            )

            # text-level order assertions: tables created before the
            # deferred foreign keys; constraints dropped before tables
            assert upgrade_src.index("create_table('a'") < upgrade_src.index(
                "create_table('c'"
            )
            assert upgrade_src.index(
                "create_table('c'"
            ) < upgrade_src.index("create_foreign_key")
            assert downgrade_src.index(
                "drop_constraint"
            ) < downgrade_src.index("drop_table")
            assert downgrade_src.index(
                "drop_table('c'"
            ) < downgrade_src.index("drop_table('a'")

            # run the SQLite-executable subset of the generated script
            source = (
                "def upgrade():\n"
                + upgrade_src
                + "\ndef downgrade():\n"
                + downgrade_src
            )
            operations = Operations(MigrationContext.configure(conn))
            namespace = {
                "op": _SQLiteSubset(operations),
                "sa": sqlalchemy,
            }
            exec(compile(source, "<migration>", "exec"), namespace)

            namespace["upgrade"]()
            eq_(set(inspect(conn).get_table_names()), {"a", "b", "c"})
            eq_(
                {col["name"] for col in inspect(conn).get_columns("c")},
                {"id", "a_id"},
            )

            namespace["downgrade"]()
            eq_(set(inspect(conn).get_table_names()), set())

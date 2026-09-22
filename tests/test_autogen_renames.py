import sqlalchemy as sa
from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import text
from sqlalchemy import UniqueConstraint

from alembic import op
from alembic.autogenerate import compare_metadata
from alembic.autogenerate import produce_migrations
from alembic.autogenerate import render_python_code
from alembic.autogenerate.renames import ColumnRenameMap
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations import ops
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing import TestBase
from alembic.testing.suite._autogen_fixtures import AutogenFixtureTest
from alembic.util import CommandError

class ColumnRenameMapTest(TestBase):
    def test_parse_mapping_key_forms(self):
        m = ColumnRenameMap.from_user_input(
            {("customer", "full_name"): "display_name"}
        )
        eq_(
            m.renames_for_table(None, "customer", None),
            {"full_name": "display_name"},
        )

        m = ColumnRenameMap.from_user_input(
            {(None, "customer", "full_name"): "display_name"}
        )
        eq_(
            m.renames_for_table(None, "customer", None),
            {"full_name": "display_name"},
        )

    def test_parse_sequence_of_pairs(self):
        m = ColumnRenameMap.from_user_input(
            [((None, "customer", "full_name"), "display_name")]
        )
        eq_(
            m.renames_for_table(None, "customer", None),
            {"full_name": "display_name"},
        )

    def test_schema_identity_resolution(self):
        m = ColumnRenameMap.from_user_input(
            {
                ("main", "customer", "a"): "b",
                (None, "customer", "c"): "d",
                ("other", "customer", "e"): "f",
            }
        )
        # "main" is the default schema here, so both entries match;
        # the "other"-schema entry does not
        eq_(
            m.renames_for_table(None, "customer", "main"),
            {"a": "b", "c": "d"},
        )
        eq_(m.renames_for_table("other", "customer", "main"), {"e": "f"})

    def test_invalid_mapping_type(self):
        assert_raises_message(
            CommandError,
            "column_renames must be a mapping",
            ColumnRenameMap.from_user_input,
            "full_name",
        )

    def test_invalid_key(self):
        assert_raises_message(
            CommandError,
            "Invalid column_renames key",
            ColumnRenameMap.from_user_input,
            {"full_name": "display_name"},
        )

    def test_one_to_many_raises(self):
        m = ColumnRenameMap.from_user_input(
            [
                ((None, "customer", "full_name"), "display_name"),
                ((None, "customer", "full_name"), "nickname"),
            ]
        )
        assert_raises_message(
            CommandError,
            "more than one target",
            m.renames_for_table,
            None,
            "customer",
            None,
        )

    def test_many_to_one_raises(self):
        m = ColumnRenameMap.from_user_input(
            {
                (None, "customer", "full_name"): "display_name",
                (None, "customer", "nickname"): "display_name",
            }
        )
        assert_raises_message(
            CommandError,
            "same target",
            m.renames_for_table,
            None,
            "customer",
            None,
        )

    def test_cycle_raises(self):
        m = ColumnRenameMap.from_user_input(
            {
                (None, "customer", "a"): "b",
                (None, "customer", "b"): "a",
            }
        )
        assert_raises_message(
            CommandError,
            "cyclic rename",
            m.renames_for_table,
            None,
            "customer",
            None,
        )

    def test_chain_is_ordered_for_execution(self):
        # a -> b -> c: the rename freeing "b" must be emitted first
        m = ColumnRenameMap.from_user_input(
            {
                (None, "customer", "a"): "b",
                (None, "customer", "b"): "c",
            }
        )
        eq_(
            list(m.renames_for_table(None, "customer", None).items()),
            [("b", "c"), ("a", "b")],
        )

    def test_unconsumed_entries_raise(self):
        m = ColumnRenameMap.from_user_input(
            {(None, "customer", "full_name"): "display_name"}
        )
        assert_raises_message(
            CommandError,
            "rename source not found",
            m.validate_all_consumed,
        )

    def test_empty_map(self):
        m = ColumnRenameMap.from_user_input(None)
        eq_(m.renames_for_table(None, "customer", None), {})
        m.validate_all_consumed()

def _customer_order_model(metadata, cust_cols, order_fk_col):
    customer = Table(
        "customer",
        metadata,
        Column("id", Integer, primary_key=True),
        *cust_cols,
    )
    Table(
        "orders",
        metadata,
        Column("id", Integer, primary_key=True),
        Column(
            "customer_name",
            String(100),
            ForeignKey("customer.%s" % order_fk_col),
        ),
        Column("amount", Integer),
    )
    return customer


def _table_ops(uo, tname):
    result = []
    for op_ in uo.ops:
        if isinstance(op_, ops.ModifyTableOps) and op_.table_name == tname:
            result.extend(op_.ops)
    return result


class AutogenRenameDiffsTest(AutogenFixtureTest, TestBase):
    __only_on__ = "sqlite"
    __backend__ = True

    def _fixture_renames(self, m1, m2, renames, **kw):
        opts = {"column_renames": renames}
        opts.update(kw.pop("extra_opts", {}))
        return self._fixture(m1, m2, opts=opts, return_ops=True, **kw)

    def test_rename_detected_instead_of_add_drop(self):
        m1 = MetaData()
        _customer_order_model(
            m1,
            [
                Column("full_name", String(100)),
                Column("email", String(100)),
            ],
            "full_name",
        )
        m2 = MetaData()
        _customer_order_model(
            m2,
            [
                Column("display_name", String(100)),
                Column("email", String(100)),
            ],
            "display_name",
        )

        uo = self._fixture_renames(
            m1, m2, {("customer", "full_name"): "display_name"}
        )
        customer_ops = _table_ops(uo, "customer")
        eq_(len(customer_ops), 1)
        alter = customer_ops[0]
        assert isinstance(alter, ops.AlterColumnOp)
        eq_(alter.column_name, "full_name")
        eq_(alter.modify_name, "display_name")

        # no add/drop anywhere
        eq_(_table_ops(uo, "orders"), [])
        assert not any(
            isinstance(op_, (ops.AddColumnOp, ops.DropColumnOp))
            for op_ in customer_ops
        )

    def test_no_rename_without_mapping(self):
        # without an explicit mapping, autogenerate must keep producing
        # drop/add; no name-similarity guessing is applied
        m1 = MetaData()
        _customer_order_model(
            m1,
            [
                Column("full_name", String(100)),
                Column("email", String(100)),
            ],
            "full_name",
        )
        m2 = MetaData()
        _customer_order_model(
            m2,
            [
                Column("display_name", String(100)),
                Column("email", String(100)),
            ],
            "display_name",
        )

        uo = self._fixture(m1, m2, return_ops=True)
        customer_ops = _table_ops(uo, "customer")
        assert any(isinstance(op_, ops.AddColumnOp) for op_ in customer_ops)
        assert any(isinstance(op_, ops.DropColumnOp) for op_ in customer_ops)
        assert not any(
            getattr(op_, "modify_name", None) for op_ in customer_ops
        )

    def test_rename_still_compares_type_and_nullable(self):
        m1 = MetaData()
        _customer_order_model(
            m1,
            [
                Column("full_name", String(100)),
                Column("email", String(100)),
            ],
            "full_name",
        )
        m2 = MetaData()
        _customer_order_model(
            m2,
            [
                Column("display_name", String(50), nullable=False),
                Column("email", String(100)),
            ],
            "display_name",
        )

        uo = self._fixture_renames(
            m1, m2, {("customer", "full_name"): "display_name"}
        )
        customer_ops = _table_ops(uo, "customer")
        eq_(len(customer_ops), 1)
        alter = customer_ops[0]
        eq_(alter.modify_name, "display_name")
        eq_(alter.modify_nullable, False)
        assert alter.modify_type is not None

    def test_rename_with_unique_constraint_and_index_preserved(self):
        m1 = MetaData()
        customer = _customer_order_model(
            m1,
            [
                Column("full_name", String(100)),
                Column("email", String(100)),
                UniqueConstraint("full_name"),
            ],
            "full_name",
        )
        Index("ix_customer_full_name", customer.c.full_name)
        m2 = MetaData()
        customer = _customer_order_model(
            m2,
            [
                Column("display_name", String(100)),
                Column("email", String(100)),
                UniqueConstraint("display_name"),
            ],
            "display_name",
        )
        Index("ix_customer_full_name", customer.c.display_name)

        uo = self._fixture_renames(
            m1, m2, {("customer", "full_name"): "display_name"}
        )
        customer_ops = _table_ops(uo, "customer")
        # only the rename; constraints and indexes follow the new name
        eq_(len(customer_ops), 1)
        assert isinstance(customer_ops[0], ops.AlterColumnOp)

    def test_fk_referring_to_renamed_column_not_touched(self):
        m1 = MetaData()
        _customer_order_model(
            m1, [Column("full_name", String(100), unique=True)], "full_name"
        )
        m2 = MetaData()
        _customer_order_model(
            m2,
            [Column("display_name", String(100), unique=True)],
            "display_name",
        )

        uo = self._fixture_renames(
            m1, m2, {("customer", "full_name"): "display_name"}
        )
        # no FK drop/add on orders
        eq_(_table_ops(uo, "orders"), [])

    def test_rename_of_fk_constrained_column(self):
        m1 = MetaData()
        _customer_order_model(
            m1, [Column("full_name", String(100), unique=True)], "full_name"
        )
        m2 = MetaData()
        customer = Table(
            "customer",
            m2,
            Column("id", Integer, primary_key=True),
            Column("full_name", String(100), unique=True),
        )
        Table(
            "orders",
            m2,
            Column("id", Integer, primary_key=True),
            Column(
                "buyer_name",
                String(100),
                ForeignKey("customer.full_name"),
            ),
            Column("amount", Integer),
        )

        uo = self._fixture_renames(
            m1, m2, {("orders", "customer_name"): "buyer_name"}
        )
        order_ops = _table_ops(uo, "orders")
        eq_(len(order_ops), 1)
        alter = order_ops[0]
        assert isinstance(alter, ops.AlterColumnOp)
        eq_(alter.modify_name, "buyer_name")
        eq_(_table_ops(uo, "customer"), [])

    def test_real_unique_constraint_change_still_detected(self):
        m1 = MetaData()
        _customer_order_model(
            m1,
            [
                Column("full_name", String(100)),
                Column("email", String(100)),
                UniqueConstraint("full_name", name="uq_customer_name"),
            ],
            "full_name",
        )
        m2 = MetaData()
        _customer_order_model(
            m2,
            [
                Column("display_name", String(100)),
                Column("email", String(100)),
            ],
            "display_name",
        )

        uo = self._fixture_renames(
            m1, m2, {("customer", "full_name"): "display_name"}
        )
        customer_ops = _table_ops(uo, "customer")
        assert any(
            isinstance(op_, ops.DropConstraintOp) for op_ in customer_ops
        )

    def test_real_fk_change_still_detected(self):
        m1 = MetaData()
        _customer_order_model(
            m1, [Column("full_name", String(100), unique=True)], "full_name"
        )
        m2 = MetaData()
        Table(
            "customer",
            m2,
            Column("id", Integer, primary_key=True),
            Column("display_name", String(100), unique=True),
        )
        # orders no longer has the FK at all
        Table(
            "orders",
            m2,
            Column("id", Integer, primary_key=True),
            Column("customer_name", String(100)),
            Column("amount", Integer),
        )

        uo = self._fixture_renames(
            m1, m2, {("customer", "full_name"): "display_name"}
        )
        order_ops = _table_ops(uo, "orders")
        assert any(
            isinstance(op_, ops.DropConstraintOp) for op_ in order_ops
        )

    def test_chained_renames_are_ordered(self):
        # conn has a, b; metadata has b, c; a -> b -> c.  the rename
        # freeing "b" must be emitted before the rename claiming it.
        m1 = MetaData()
        Table(
            "customer",
            m1,
            Column("id", Integer, primary_key=True),
            Column("a", String(100)),
            Column("b", String(100)),
        )
        m2 = MetaData()
        Table(
            "customer",
            m2,
            Column("id", Integer, primary_key=True),
            Column("b", String(100)),
            Column("c", String(100)),
        )

        uo = self._fixture_renames(
            m1, m2, {("customer", "a"): "b", ("customer", "b"): "c"}
        )
        customer_ops = _table_ops(uo, "customer")
        eq_(len(customer_ops), 2)
        eq_(
            [(op_.column_name, op_.modify_name) for op_ in customer_ops],
            [("b", "c"), ("a", "b")],
        )

    def test_rename_and_readd_vacated_name(self):
        # rename full_name -> display_name, and add a brand new column
        # that reuses the full_name name
        m1 = MetaData()
        Table(
            "customer",
            m1,
            Column("id", Integer, primary_key=True),
            Column("full_name", String(100)),
        )
        m2 = MetaData()
        Table(
            "customer",
            m2,
            Column("id", Integer, primary_key=True),
            Column("display_name", String(100)),
            Column("full_name", Integer),
        )

        uo = self._fixture_renames(
            m1, m2, {("customer", "full_name"): "display_name"}
        )
        customer_ops = _table_ops(uo, "customer")
        eq_(len(customer_ops), 2)
        # the rename comes first, so the vacated name can be re-added
        rename_op, add_op = customer_ops
        assert isinstance(rename_op, ops.AlterColumnOp)
        eq_(rename_op.modify_name, "display_name")
        assert isinstance(add_op, ops.AddColumnOp)
        eq_(add_op.column.name, "full_name")

    def test_source_column_not_found_raises(self):
        m1 = MetaData()
        _customer_order_model(
            m1, [Column("full_name", String(100))], "full_name"
        )
        m2 = MetaData()
        _customer_order_model(
            m2, [Column("display_name", String(100))], "display_name"
        )

        assert_raises_message(
            CommandError,
            "source column that does not exist",
            self._fixture_renames,
            m1,
            m2,
            {("customer", "nope"): "display_name"},
        )

    def test_target_occupied_raises(self):
        m1 = MetaData()
        _customer_order_model(
            m1,
            [
                Column("full_name", String(100)),
                Column("display_name", String(100)),
            ],
            "full_name",
        )
        m2 = MetaData()
        _customer_order_model(
            m2, [Column("display_name", String(100))], "display_name"
        )

        assert_raises_message(
            CommandError,
            "occupied by an unrelated existing column",
            self._fixture_renames,
            m1,
            m2,
            {("customer", "full_name"): "display_name"},
        )

    def test_cycle_raises(self):
        m1 = MetaData()
        _customer_order_model(
            m1,
            [Column("a", String(100)), Column("b", String(100))],
            "a",
        )
        m2 = MetaData()
        _customer_order_model(
            m2,
            [Column("a", String(100)), Column("b", String(100))],
            "a",
        )

        assert_raises_message(
            CommandError,
            "cyclic rename",
            self._fixture_renames,
            m1,
            m2,
            {("customer", "a"): "b", ("customer", "b"): "a"},
        )

    def test_one_to_many_raises(self):
        m1 = MetaData()
        _customer_order_model(
            m1, [Column("full_name", String(100))], "full_name"
        )
        m2 = MetaData()
        _customer_order_model(
            m2,
            [Column("display_name", String(100)), Column("nick", String(100))],
            "display_name",
        )

        assert_raises_message(
            CommandError,
            "more than one target",
            self._fixture_renames,
            m1,
            m2,
            [
                ((None, "customer", "full_name"), "display_name"),
                ((None, "customer", "full_name"), "nick"),
            ],
        )

    def test_unknown_table_raises(self):
        m1 = MetaData()
        _customer_order_model(
            m1, [Column("full_name", String(100))], "full_name"
        )
        m2 = MetaData()
        _customer_order_model(
            m2, [Column("display_name", String(100))], "display_name"
        )

        assert_raises_message(
            CommandError,
            "rename source not found",
            self._fixture_renames,
            m1,
            m2,
            {("nonexistent", "full_name"): "display_name"},
        )

    def test_target_not_in_metadata_raises(self):
        m1 = MetaData()
        _customer_order_model(
            m1, [Column("full_name", String(100))], "full_name"
        )
        m2 = MetaData()
        # metadata has neither the source nor the target name
        Table(
            "customer",
            m2,
            Column("id", Integer, primary_key=True),
            Column("email", String(100)),
        )
        Table(
            "orders",
            m2,
            Column("id", Integer, primary_key=True),
            Column("customer_name", String(100)),
            Column("amount", Integer),
        )

        assert_raises_message(
            CommandError,
            "not present in the target metadata",
            self._fixture_renames,
            m1,
            m2,
            {("customer", "full_name"): "display_name"},
        )

    def test_compare_metadata_reports_rename_diff(self):
        m1 = MetaData()
        _customer_order_model(
            m1, [Column("full_name", String(100))], "full_name"
        )
        m2 = MetaData()
        _customer_order_model(
            m2, [Column("display_name", String(100))], "display_name"
        )
        uo = self._fixture_renames(
            m1, m2, {("customer", "full_name"): "display_name"}
        )
        diffs = uo.as_diffs()
        flat = [
            item
            for entry in diffs
            for item in (entry if isinstance(entry, list) else [entry])
        ]
        rename_diffs = [d for d in flat if d[0] == "rename_column"]
        eq_(
            rename_diffs,
            [("rename_column", None, "customer", "full_name", "display_name")],
        )

class SQLiteRenameRoundTripTest(TestBase):
    """Generate a migration for a rename against a SQLite database that
    holds data and cross-table foreign keys, then run upgrade and
    downgrade, verifying that data is preserved and that a repeated
    autogenerate produces no further diffs.

    """

    __only_on__ = "sqlite"

    renames = {(None, "customer", "full_name"): "display_name"}

    def _v1_schema(self):
        m = MetaData()
        customer = Table(
            "customer",
            m,
            Column("id", Integer, primary_key=True),
            Column("full_name", String(100)),
            Column("email", String(100)),
            UniqueConstraint("full_name", name="uq_customer_full_name"),
        )
        Index("ix_customer_full_name", customer.c.full_name)
        Table(
            "orders",
            m,
            Column("id", Integer, primary_key=True),
            Column(
                "customer_name",
                String(100),
                ForeignKey("customer.full_name"),
            ),
            Column("amount", Integer),
        )
        return m

    def _v2_schema(self):
        m = MetaData()
        customer = Table(
            "customer",
            m,
            Column("id", Integer, primary_key=True),
            Column("display_name", String(100)),
            Column("email", String(100)),
            UniqueConstraint("display_name", name="uq_customer_full_name"),
        )
        Index("ix_customer_full_name", customer.c.display_name)
        Table(
            "orders",
            m,
            Column("id", Integer, primary_key=True),
            Column(
                "customer_name",
                String(100),
                ForeignKey("customer.display_name"),
            ),
            Column("amount", Integer),
        )
        return m

    def _populate(self, conn, m1):
        conn.execute(
            m1.tables["customer"].insert(),
            [
                {"id": 1, "full_name": "Alice", "email": "alice@example.com"},
                {"id": 2, "full_name": "Bob", "email": "bob@example.com"},
            ],
        )
        conn.execute(
            m1.tables["orders"].insert(),
            [
                {"id": 1, "customer_name": "Alice", "amount": 10},
                {"id": 2, "customer_name": "Bob", "amount": 20},
            ],
        )

    def _autogen(self, conn, metadata):
        context = MigrationContext.configure(
            connection=conn,
            opts={
                "target_metadata": metadata,
                "compare_type": True,
                "column_renames": self.renames,
            },
        )
        return produce_migrations(context, metadata)

    def _run_script(self, conn, code):
        migration_context = MigrationContext.configure(connection=conn)
        glb = {"op": op, "sa": sa}
        with Operations.context(migration_context):
            exec(
                compile(
                    "def migrate():\n" + code, "<migration>", "exec"
                ),
                glb,
            )
            glb["migrate"]()

    def _customer_names(self, conn, column):
        return [
            row[0]
            for row in conn.execute(
                text("select %s from customer order by id" % column)
            )
        ]

    def _referred_columns(self, conn):
        return [
            row[4]
            for row in conn.execute(text("pragma foreign_key_list(orders)"))
        ]

    def _unique_indexed_columns(self, conn):
        result = set()
        for row in conn.execute(text("pragma index_list(customer)")):
            if row[2]:  # unique flag
                for col_row in conn.execute(
                    text("pragma index_info(%s)" % row[1])
                ):
                    result.add(col_row[2])
        return result

    def _test_roundtrip(self, render_as_batch):
        engine = create_engine("sqlite://")
        conn = engine.connect()
        m1 = self._v1_schema()
        m2 = self._v2_schema()
        m1.create_all(conn)
        self._populate(conn, m1)

        # 1. generate the migration
        migration_script = self._autogen(conn, m2)
        upgrade_code = render_python_code(
            migration_script.upgrade_ops, render_as_batch=render_as_batch
        )
        downgrade_code = render_python_code(
            migration_script.downgrade_ops, render_as_batch=render_as_batch
        )

        # the migration is a rename, not a drop/add
        assert "new_column_name='display_name'" in upgrade_code, upgrade_code
        assert "add_column" not in upgrade_code, upgrade_code
        assert "drop_column" not in upgrade_code, upgrade_code
        # downgrade renames back
        assert "new_column_name='full_name'" in downgrade_code, downgrade_code

        # compare_metadata exposes the rename as a diff as well
        diffs = compare_metadata(
            MigrationContext.configure(
                connection=conn,
                opts={
                    "target_metadata": m2,
                    "column_renames": self.renames,
                },
            ),
            m2,
        )
        flat = [
            item
            for entry in diffs
            for item in (entry if isinstance(entry, list) else [entry])
        ]
        assert (
            "rename_column",
            None,
            "customer",
            "full_name",
            "display_name",
        ) in flat

        # 2. upgrade; data must be preserved
        self._run_script(conn, upgrade_code)
        eq_(self._customer_names(conn, "display_name"), ["Alice", "Bob"])
        eq_(
            conn.execute(
                text("select customer_name, amount from orders order by id")
            ).all(),
            [("Alice", 10), ("Bob", 20)],
        )
        # the unique constraint and index follow the renamed column
        assert "display_name" in self._unique_indexed_columns(conn)
        if not render_as_batch:
            # a native SQLite RENAME COLUMN also updates foreign key
            # references in other tables
            eq_(self._referred_columns(conn), ["display_name"])

        # 3. autogenerate again: no spurious diffs
        assert self._autogen(conn, m2).upgrade_ops.is_empty()

        # 4. downgrade; data must be preserved
        self._run_script(conn, downgrade_code)
        eq_(self._customer_names(conn, "full_name"), ["Alice", "Bob"])
        eq_(self._referred_columns(conn), ["full_name"])
        assert "full_name" in self._unique_indexed_columns(conn)

        # 5. autogenerate against the v1 model is also clean
        assert self._autogen(conn, m1).upgrade_ops.is_empty()

        # 6. upgrade once more to prove both directions are repeatable
        self._run_script(conn, upgrade_code)
        eq_(self._customer_names(conn, "display_name"), ["Alice", "Bob"])
        assert self._autogen(conn, m2).upgrade_ops.is_empty()

        conn.close()

    def test_roundtrip(self):
        self._test_roundtrip(render_as_batch=False)

    def test_roundtrip_batch(self):
        self._test_roundtrip(render_as_batch=True)

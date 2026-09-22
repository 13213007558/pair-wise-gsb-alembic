import json
from pathlib import Path

import pytest

from alembic import command
from alembic.script import MigrationPlan
from alembic.script import PlanStep
from alembic.script import ScriptDirectory
from alembic.script import VersionTableOp
from alembic.script.revision import CycleDetected
from alembic.script.revision import MultipleHeads
from alembic.script.revision import ResolutionError
from alembic.script.revision import RevisionError
from alembic.testing import assert_raises
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing.env import _get_staging_directory
from alembic.testing.env import _sqlite_testing_config
from alembic.testing.env import clear_staging_env
from alembic.testing.env import env_file_fixture
from alembic.testing.env import staging_env
from alembic.testing.fixtures import TestBase

# a graph exercising multiple bases, a branch, a merge revision,
# branch labels, and a depends_on-only branch:
#
#   a1 -> a2 -> a3 --+
#           |        +-> m1        (main / feature branches, merge)
#           +-> b1 -> b2 --+
#   z1 -> z2                (data branch, second base)
#   r1 -> r2                (reports branch, base depends_on z2)
GRAPH = [
    ("a1", None, None, "main"),
    ("a2", "a1", None, None),
    ("a3", "a2", None, None),
    ("b1", "a2", None, "feature"),
    ("b2", "b1", None, None),
    ("m1", ("a3", "b2"), None, None),
    ("z1", None, None, "data"),
    ("z2", "z1", None, None),
    ("r1", None, "z2", "reports"),
    ("r2", "r1", None, None),
]

ALL_REVISIONS = {rev for rev, _, _, _ in GRAPH}


def _write_graph(versions: Path) -> None:
    for rev, down, deps, labels in GRAPH:
        lines = [
            '"Rev %s"' % rev,
            "revision = %r" % (rev,),
            "down_revision = %r" % (down,),
        ]
        if deps is not None:
            lines.append("depends_on = %r" % (deps,))
        if labels is not None:
            lines.append("branch_labels = %r" % (labels,))
        lines += [
            "",
            "def upgrade():",
            "    pass",
            "",
            "def downgrade():",
            "    pass",
            "",
        ]
        (versions / ("%s.py" % rev)).write_text("\n".join(lines))


def _apply_ops(heads, ops):
    heads = set(heads)
    for op in ops:
        if op.operation == "insert":
            heads.add(op.revision)
        elif op.operation == "delete":
            heads.remove(op.revision)
        elif op.operation == "update":
            heads.remove(op.from_revision)
            heads.add(op.to_revision)
        else:  # pragma: no cover
            raise AssertionError("unknown op %r" % op.operation)
    return heads


def _assert_plan_consistent(plan: MigrationPlan):
    eq_(plan.initial_heads, plan.steps[0].heads_before)
    eq_(plan.final_heads, plan.steps[-1].heads_after)
    for previous, next_ in zip(plan.steps, plan.steps[1:]):
        eq_(previous.heads_after, next_.heads_before)
    for step in plan.steps:
        eq_(
            tuple(
                sorted(_apply_ops(step.heads_before, step.version_table_ops))
            ),
            step.heads_after,
        )


@pytest.fixture
def script_dir(tmp_path):
    versions = tmp_path / "versions"
    versions.mkdir()
    _write_graph(versions)
    return ScriptDirectory(str(tmp_path))


class PlanStructureTest(TestBase):
    def _by_rev(self, plan):
        return {step.revision: step for step in plan.steps}

    def test_upgrade_base_to_heads(self, script_dir):
        plan = script_dir.plan_migration((), "heads", "upgrade")
        eq_(plan.direction, "upgrade")
        eq_(plan.initial_heads, ())
        eq_(plan.final_heads, ("m1", "r2"))
        eq_({step.revision for step in plan.steps}, ALL_REVISIONS)
        eq_({step.direction for step in plan.steps}, {"upgrade"})
        _assert_plan_consistent(plan)

        steps = self._by_rev(plan)
        # first base is an insert into an empty version table
        eq_(
            steps["a1"].version_table_ops,
            (VersionTableOp("insert", revision="a1"),),
        )
        eq_(steps["a1"].heads_before, ())
        # second base inserts a new row as well
        eq_(
            steps["z1"].version_table_ops,
            (VersionTableOp("insert", revision="z1"),),
        )
        # linear steps are updates
        eq_(
            steps["a2"].version_table_ops,
            (VersionTableOp("update", from_revision="a1", to_revision="a2"),),
        )
        # the merge deletes one branch head and updates the other
        merge_ops = steps["m1"].version_table_ops
        eq_(len(merge_ops), 2)
        eq_(merge_ops[0].operation, "delete")
        eq_(merge_ops[1].operation, "update")
        eq_(merge_ops[1].to_revision, "m1")
        eq_(
            {merge_ops[0].revision, merge_ops[1].from_revision},
            {"a3", "b2"},
        )
        eq_(steps["m1"].heads_after, ("m1", "r2"))

    def test_downgrade_heads_to_base(self, script_dir):
        plan = script_dir.plan_migration(("m1", "r2"), "base", "downgrade")
        eq_(plan.direction, "downgrade")
        eq_(plan.initial_heads, ("m1", "r2"))
        eq_(plan.final_heads, ())
        eq_({step.revision for step in plan.steps}, ALL_REVISIONS)
        eq_({step.direction for step in plan.steps}, {"downgrade"})
        _assert_plan_consistent(plan)

        steps = self._by_rev(plan)
        # unmerging re-inserts one branch head and updates the merge rev
        unmerge_ops = steps["m1"].version_table_ops
        eq_(len(unmerge_ops), 2)
        eq_(unmerge_ops[0].operation, "insert")
        eq_(unmerge_ops[1].operation, "update")
        eq_(unmerge_ops[1].from_revision, "m1")
        eq_(steps["m1"].heads_after, ("a3", "b2", "r2"))
        # downgrading a base deletes its row
        eq_(
            steps["a1"].version_table_ops,
            (VersionTableOp("delete", revision="a1"),),
        )
        eq_(steps["a1"].heads_after, ())

    def test_down_revision_vs_depends_on(self, script_dir):
        plan = script_dir.plan_migration((), "heads", "upgrade")
        steps = self._by_rev(plan)
        # r1 hangs off z2 only via depends_on
        eq_(steps["r1"].down_revision, ())
        eq_(steps["r1"].depends_on, ("z2",))
        eq_(steps["r1"].branch_labels, ("reports",))
        # m1 is a real merge on down_revision only
        eq_(set(steps["m1"].down_revision), {"a3", "b2"})
        eq_(steps["m1"].depends_on, ())
        # branch labels accumulate along the lineage: b1 declares
        # 'feature' and inherits 'main' from its ancestors
        eq_(steps["b1"].branch_labels, ("feature", "main"))
        eq_(steps["b1"].down_revision, ("a2",))

    def test_relative_and_symbolic_targets(self, script_dir):
        plan = script_dir.plan_migration(("a1",), "+1", "upgrade")
        eq_([step.revision for step in plan.steps], ["a2"])

        plan = script_dir.plan_migration(("r2",), "-1", "downgrade")
        eq_([step.revision for step in plan.steps], ["r2"])
        eq_(plan.final_heads, ("r1",))

        plan = script_dir.plan_migration((), "data@head", "upgrade")
        eq_([step.revision for step in plan.steps], ["z1", "z2"])

        plan = script_dir.plan_migration((), "reports@head", "upgrade")
        eq_(
            {step.revision for step in plan.steps},
            {"z1", "z2", "r1", "r2"},
        )

        plan = script_dir.plan_migration(("a2",), "b2", "upgrade")
        eq_([step.revision for step in plan.steps], ["b1", "b2"])

    def test_noop_plans(self, script_dir):
        plan = script_dir.plan_migration(("m1", "r2"), "heads", "upgrade")
        eq_(plan.steps, ())
        eq_(plan.final_heads, ("m1", "r2"))

        plan = script_dir.plan_migration((), "base", "downgrade")
        eq_(plan.steps, ())
        eq_(plan.final_heads, ())

    def test_plan_is_json_serializable(self, script_dir):
        plan = script_dir.plan_migration((), "heads", "upgrade")
        as_dict = plan.as_dict()
        json.dumps(as_dict)
        eq_(as_dict["direction"], "upgrade")
        eq_(as_dict["target"], "heads")
        eq_(as_dict["final_heads"], ["m1", "r2"])
        eq_(len(as_dict["steps"]), len(ALL_REVISIONS))
        step = as_dict["steps"][0]
        for key in (
            "revision",
            "direction",
            "down_revision",
            "depends_on",
            "branch_labels",
            "heads_before",
            "heads_after",
            "version_table_ops",
        ):
            assert key in step, key

    def test_plan_does_not_mutate_inputs_or_disk(
        self, script_dir, tmp_path
    ):
        versions = tmp_path / "versions"
        before = {
            path.name: path.read_bytes()
            for path in versions.glob("*.py")
        }
        current = ["m1", "r2"]
        plan = script_dir.plan_migration(current, "base", "downgrade")
        eq_(len(plan.steps), len(ALL_REVISIONS))
        eq_(current, ["m1", "r2"])
        after = {
            path.name: path.read_bytes()
            for path in versions.glob("*.py")
        }
        eq_(after, before)


class PlanErrorTest(TestBase):
    def test_ambiguous_target(self, script_dir):
        assert_raises(
            MultipleHeads,
            script_dir.plan_migration,
            ("a2",),
            "head",
            "upgrade",
        )

    def test_ambiguous_relative_target(self, script_dir):
        assert_raises_message(
            RevisionError,
            "Ambiguous walk",
            script_dir.plan_migration,
            ("a2",),
            "+1",
            "upgrade",
        )

    def test_unknown_current_revision(self, script_dir):
        assert_raises_message(
            ResolutionError,
            "No such revision or branch 'nonexistent'",
            script_dir.plan_migration,
            ("nonexistent",),
            "heads",
            "upgrade",
        )

    def test_unknown_target(self, script_dir):
        assert_raises(
            ResolutionError,
            script_dir.plan_migration,
            (),
            "nonexistent",
            "upgrade",
        )

    def test_duplicate_current_revisions(self, script_dir):
        assert_raises_message(
            RevisionError,
            "contains duplicates: a2",
            script_dir.plan_migration,
            ("a2", "a2"),
            "heads",
            "upgrade",
        )

    def test_current_heads_not_a_valid_state(self, script_dir):
        assert_raises_message(
            RevisionError,
            "a2 is already implied by a3",
            script_dir.plan_migration,
            ("a2", "a3"),
            "heads",
            "upgrade",
        )

    def test_base_is_not_a_current_revision(self, script_dir):
        assert_raises_message(
            RevisionError,
            "'base' is not a valid current revision",
            script_dir.plan_migration,
            ("base",),
            "heads",
            "upgrade",
        )

    def test_invalid_direction(self, script_dir):
        assert_raises(
            ValueError,
            script_dir.plan_migration,
            (),
            "heads",
            "sideways",
        )

    def test_target_not_a_descendant(self, script_dir):
        assert_raises(
            RevisionError,
            script_dir.plan_migration,
            ("a1",),
            "a3",
            "downgrade",
        )

    def test_down_revision_cycle(self, tmp_path):
        versions = tmp_path / "versions"
        versions.mkdir()
        for rev, down in (("x1", "y1"), ("y1", "x1")):
            (versions / ("%s.py" % rev)).write_text(
                "revision = %r\n"
                "down_revision = %r\n"
                "\n"
                "def upgrade():\n"
                "    pass\n"
                "\n"
                "def downgrade():\n"
                "    pass\n" % (rev, down)
            )
        sd = ScriptDirectory(str(tmp_path))
        assert_raises(
            CycleDetected, sd.plan_migration, (), "heads", "upgrade"
        )

    def test_depends_on_cycle(self, tmp_path):
        versions = tmp_path / "versions"
        versions.mkdir()
        for rev, deps in (("x1", "y1"), ("y1", "x1")):
            (versions / ("%s.py" % rev)).write_text(
                "revision = %r\n"
                "down_revision = None\n"
                "depends_on = %r\n"
                "\n"
                "def upgrade():\n"
                "    pass\n"
                "\n"
                "def downgrade():\n"
                "    pass\n" % (rev, deps)
            )
        sd = ScriptDirectory(str(tmp_path))
        assert_raises(
            CycleDetected, sd.plan_migration, (), "heads", "upgrade"
        )


class PlanAgainstSQLiteTest(TestBase):
    """Run the same branched migrations against a temporary SQLite
    database and compare the real version table contents, step by
    step, with the computed plan."""

    @classmethod
    def setup_class(cls):
        cls.env = staging_env()
        cls.cfg = _sqlite_testing_config()
        versions = (
            Path(_get_staging_directory()) / "scripts" / "versions"
        )
        _write_graph(versions)
        env_file_fixture(
            """
from sqlalchemy import create_engine
from sqlalchemy import text


def _version_rows(ctx):
    return sorted(
        row[0]
        for row in ctx.connection.execute(
            text("select version_num from alembic_version")
        )
    )


def _record(**kw):
    config.attributes["applied"].append(
        {
            "revision": kw["step"].up_revision_id,
            "heads": sorted(kw["heads"]),
            "db_rows": _version_rows(kw["ctx"]),
        }
    )


eng = create_engine(config.get_main_option("sqlalchemy.url"))
with eng.connect() as connection:
    context.configure(connection=connection, on_version_apply=_record)
    with context.begin_transaction():
        context.run_migrations()
"""
        )
        cls.script = ScriptDirectory.from_config(cls.cfg)

    @classmethod
    def teardown_class(cls):
        clear_staging_env()

    def _run_and_compare(self, current, target, direction):
        self.cfg.attributes["applied"] = []
        plan = self.script.plan_migration(current, target, direction)
        if direction == "upgrade":
            command.upgrade(self.cfg, target)
        else:
            command.downgrade(self.cfg, target)
        applied = self.cfg.attributes["applied"]

        eq_(
            [entry["revision"] for entry in applied],
            [step.revision for step in plan.steps],
        )
        for step, entry in zip(plan.steps, applied):
            eq_(
                tuple(entry["heads"]),
                step.heads_after,
                "head maintainer heads after %s" % step.revision,
            )
            eq_(
                tuple(entry["db_rows"]),
                step.heads_after,
                "version table rows after %s" % step.revision,
            )
        return plan

    def test_upgrade_and_downgrade_match_plan(self):
        plan = self._run_and_compare((), "heads", "upgrade")
        eq_(plan.final_heads, ("m1", "r2"))

        plan = self._run_and_compare(plan.final_heads, "base", "downgrade")
        eq_(plan.final_heads, ())

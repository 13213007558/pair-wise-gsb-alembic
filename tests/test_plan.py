"""Tests for the offline migration planning API in alembic.plan."""

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text

from alembic.plan import MigrationPlan
from alembic.plan import plan_migrations
from alembic.plan import PlanStep
from alembic.plan import VersionTableChange
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.testing.fixtures import TestBase
from alembic.util import CommandError

REVISION_TEMPLATE = '''"""{doc}"""

revision = {revision!r}
down_revision = {down_revision!r}
branch_labels = {branch_labels!r}
depends_on = {depends_on!r}


def upgrade():
    pass


def downgrade():
    pass
'''


def _write_revision(
    versions: Path,
    revision: str,
    down_revision,
    branch_labels=None,
    depends_on=None,
    doc=None,
):
    content = REVISION_TEMPLATE.format(
        revision=revision,
        down_revision=down_revision,
        branch_labels=branch_labels,
        depends_on=depends_on,
        doc=doc or revision,
    )
    (versions / ("%s.py" % revision)).write_text(content)


def _make_script_directory(tmp_path: Path, revisions) -> ScriptDirectory:
    root = tmp_path / "scripts"
    versions = root / "versions"
    versions.mkdir(parents=True)
    for spec in revisions:
        _write_revision(versions, *spec)
    return ScriptDirectory(dir=str(root))


@pytest.fixture
def branched_revisions():
    """A graph with two bases, a cross-branch dependency and a merge.

    a1 (base, label "main") -> a2 --+
                                    +-> m1 (merge)
    b1 (base, label "feature") -> b2 (depends_on a1)

    """
    return [
        ("a1", None, ("main",), None, "base a1"),
        ("a2", "a1", None, None, "a2"),
        ("b1", None, ("feature",), None, "base b1"),
        ("b2", "b1", None, "a1", "b2 depends on a1"),
        ("m1", ("a2", "b2"), None, None, "merge a2 and b2"),
    ]


@pytest.fixture
def script_directory(tmp_path, branched_revisions):
    return _make_script_directory(tmp_path, branched_revisions)


def _version_table_hashes(script_directory: ScriptDirectory):
    versions = Path(script_directory.versions)
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(versions.glob("*.py"))
    }


def _run_steps_on_sqlite(script_directory, db_path, steps):
    """Run real migration steps against a temp SQLite database.

    Returns the ordered list of version table contents (as sets)
    captured after each step was applied.

    """
    engine = create_engine("sqlite:///%s" % db_path)
    captured = []
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={
                "fn": lambda rev, ctx: steps,
                "on_version_apply": [lambda ctx, step, heads, run_args: (
                    captured.append(
                        {
                            row[0]
                            for row in connection.execute(
                                text(
                                    "select version_num from alembic_version"
                                )
                            )
                        }
                    )
                )],
            },
        )
        context.run_migrations()
    engine.dispose()
    return captured


def _upgrade_steps(script_directory, target, current):
    return script_directory._upgrade_revs(target, tuple(current))


def _downgrade_steps(script_directory, target, current):
    return script_directory._downgrade_revs(target, tuple(current))


def _assert_plan_matches_capture(plan, captured):
    assert len(plan.steps) == len(captured)
    for step, rows in zip(plan.steps, captured):
        assert set(step.heads_after) == rows
    if plan.steps:
        assert set(plan.final_heads) == captured[-1]


def _assert_plan_is_chained(plan):
    for previous, step in zip(plan.steps, plan.steps[1:]):
        assert step.heads_before == previous.heads_after
    if plan.steps:
        assert plan.steps[0].heads_before == plan.initial_heads
        assert plan.steps[-1].heads_after == plan.final_heads

class TestUpgradePlan(TestBase):
    def test_steps_and_version_table_changes(self, script_directory):
        plan = plan_migrations(script_directory, None, "heads", "upgrade")

        assert isinstance(plan, MigrationPlan)
        assert plan.direction == "upgrade"
        assert plan.initial_heads == ()
        assert plan.final_heads == ("m1",)
        assert [step.revision for step in plan.steps] == [
            "a1",
            "b1",
            "b2",
            "a2",
            "m1",
        ]
        _assert_plan_is_chained(plan)

        by_revision = {step.revision: step for step in plan.steps}

        # base revisions create new branch rows
        assert by_revision["a1"].version_table_change == VersionTableChange(
            operation="insert", inserts=("a1",)
        )
        assert by_revision["b1"].version_table_change.operation == "insert"

        # b2 depends on a1, and a1's row is present when b2 is
        # applied, so applying b2 merges that row away
        b2_change = by_revision["b2"].version_table_change
        assert b2_change.operation == "merge"
        assert b2_change.deletes == ("b1",)
        assert b2_change.update == ("a1", "b2")

        # a2 is then inserted as a new branch row
        assert by_revision["a2"].version_table_change == VersionTableChange(
            operation="insert", inserts=("a2",)
        )

        # the merge revision deletes one row and updates the other
        merge_change = by_revision["m1"].version_table_change
        assert merge_change.operation == "merge"
        assert merge_change.deletes == ("a2",)
        assert merge_change.update == ("b2", "m1")

        # down_revision vs depends_on distinction is preserved
        assert by_revision["b2"].down_revisions == ("b1",)
        assert by_revision["b2"].depends_on == ("a1",)
        assert by_revision["m1"].down_revisions == ("a2", "b2")
        assert by_revision["m1"].depends_on == ()

        # branch labels are exposed
        assert by_revision["a1"].branch_labels == ("main",)
        assert by_revision["b1"].branch_labels == ("feature",)

        # heads before/after each step
        assert by_revision["a1"].heads_before == ()
        assert by_revision["a1"].heads_after == ("a1",)
        assert by_revision["m1"].heads_before == ("a2", "b2")
        assert by_revision["m1"].heads_after == ("m1",)

    def test_matches_real_sqlite_run(self, script_directory, tmp_path):
        plan = plan_migrations(script_directory, [], "heads", "upgrade")
        steps = _upgrade_steps(script_directory, "heads", ())
        captured = _run_steps_on_sqlite(
            script_directory, tmp_path / "up.db", steps
        )
        _assert_plan_matches_capture(plan, captured)

    def test_branch_label_target(self, tmp_path, branched_revisions):
        # drop the merge revision so that "feature@head" resolves to b2;
        # with the merge present, both branch labels propagate to m1
        revisions = [r for r in branched_revisions if r[0] != "m1"]
        sd = _make_script_directory(tmp_path, revisions)

        plan = plan_migrations(sd, None, "feature@head", "upgrade")
        # upgrading the feature branch pulls in the "main" base a1
        # because b2 depends on it
        assert [step.revision for step in plan.steps] == ["a1", "b1", "b2"]
        # applying b2 merges away the a1 row it depends on
        assert plan.steps[-1].version_table_change.operation == "merge"
        assert plan.final_heads == ("b2",)
        _assert_plan_is_chained(plan)

        steps = _upgrade_steps(sd, "feature@head", ())
        captured = _run_steps_on_sqlite(sd, tmp_path / "branch.db", steps)
        _assert_plan_matches_capture(plan, captured)

    def test_branch_label_propagates_to_merge(self, script_directory):
        # the merge revision carries both branch labels, so either
        # branch@head resolves to it
        plan = plan_migrations(
            script_directory, None, "feature@head", "upgrade"
        )
        assert plan.final_heads == ("m1",)

    def test_relative_target(self, script_directory):
        plan = plan_migrations(script_directory, {"a1"}, "+1", "upgrade")
        assert [step.revision for step in plan.steps] == ["a2"]
        assert plan.initial_heads == ("a1",)
        assert plan.final_heads == ("a2",)

    def test_partial_revision_target(self, script_directory):
        plan = plan_migrations(script_directory, None, "a2", "upgrade")
        assert [step.revision for step in plan.steps] == ["a1", "a2"]

    def test_plan_method_on_script_directory(self, script_directory):
        plan = script_directory.plan(None, "heads", direction="upgrade")
        assert plan.final_heads == ("m1",)


class TestDowngradePlan(TestBase):
    def test_downgrade_to_base(self, script_directory, tmp_path):
        # first really upgrade the database all the way
        up_steps = _upgrade_steps(script_directory, "heads", ())
        _run_steps_on_sqlite(script_directory, tmp_path / "down.db", up_steps)

        plan = plan_migrations(script_directory, {"m1"}, "base", "downgrade")
        assert plan.initial_heads == ("m1",)
        assert plan.final_heads == ()
        assert [step.revision for step in plan.steps] == [
            "m1",
            "a2",
            "b2",
            "b1",
            "a1",
        ]
        _assert_plan_is_chained(plan)

        by_revision = {step.revision: step for step in plan.steps}

        # the merge is unmerged: one row updated, one row inserted
        unmerge = by_revision["m1"].version_table_change
        assert unmerge.operation == "unmerge"
        assert unmerge.update is not None
        assert unmerge.update[0] == "m1"
        assert len(unmerge.inserts) == 1

        # base revisions are deleted from the version table
        assert by_revision["a1"].version_table_change.operation == "delete"
        assert by_revision["b1"].version_table_change.operation == "delete"

        # a2's row is deleted outright: its down_revision a1 is
        # still covered by b2, which depends on it
        assert by_revision["a2"].version_table_change == VersionTableChange(
            operation="delete", deletes=("a2",)
        )

        down_steps = _downgrade_steps(script_directory, "base", ("m1",))
        captured = _run_steps_on_sqlite(
            script_directory, tmp_path / "down.db", down_steps
        )
        _assert_plan_matches_capture(plan, captured)

    def test_relative_downgrade(self, script_directory, tmp_path):
        up_steps = _upgrade_steps(script_directory, "a2", ())
        _run_steps_on_sqlite(script_directory, tmp_path / "rel.db", up_steps)

        plan = plan_migrations(script_directory, {"a2"}, "-1", "downgrade")
        assert [step.revision for step in plan.steps] == ["a2"]
        assert plan.steps[0].version_table_change == VersionTableChange(
            operation="update", update=("a2", "a1")
        )
        assert plan.final_heads == ("a1",)

        down_steps = _downgrade_steps(script_directory, "-1", ("a2",))
        captured = _run_steps_on_sqlite(
            script_directory, tmp_path / "rel.db", down_steps
        )
        _assert_plan_matches_capture(plan, captured)

    def test_relative_downgrade_from_merge_is_ambiguous(
        self, script_directory
    ):
        with pytest.raises(CommandError, match="Ambiguous walk"):
            plan_migrations(script_directory, {"m1"}, "-1", "downgrade")


class TestPlanErrors(TestBase):
    def test_invalid_direction(self, script_directory):
        with pytest.raises(CommandError, match="direction must be"):
            plan_migrations(script_directory, None, "heads", "sideways")

    def test_unknown_current_revision(self, script_directory):
        with pytest.raises(
            CommandError, match="Can't locate revision identified by 'zzz'"
        ):
            plan_migrations(script_directory, {"zzz"}, "heads", "upgrade")

    def test_unknown_target(self, script_directory):
        with pytest.raises(
            CommandError, match="Can't locate revision identified by 'zzz'"
        ):
            plan_migrations(script_directory, None, "zzz", "upgrade")

    def test_ambiguous_target(self, tmp_path):
        sd = _make_script_directory(
            tmp_path, [("aaaa1", None), ("aaaa2", "aaaa1")]
        )
        with pytest.raises(
            CommandError, match="Multiple revisions start with 'aaaa'"
        ):
            plan_migrations(sd, None, "aaaa", "upgrade")

    def test_multiple_heads_target(self, tmp_path, branched_revisions):
        # drop the merge revision so two heads remain
        revisions = [r for r in branched_revisions if r[0] != "m1"]
        sd = _make_script_directory(tmp_path, revisions)
        with pytest.raises(CommandError, match="Multiple head"):
            plan_migrations(sd, None, "head", "upgrade")

    def test_cycle_detected(self, tmp_path):
        sd = _make_script_directory(tmp_path, [("x1", "x2"), ("x2", "x1")])
        with pytest.raises(CommandError, match="[Cc]ycle"):
            plan_migrations(sd, None, "heads", "upgrade")

    def test_current_heads_with_ancestor_conflict(self, script_directory):
        with pytest.raises(
            CommandError, match="not a valid set of heads.*'a1'.*'a2'"
        ):
            plan_migrations(
                script_directory, {"a1", "a2"}, "heads", "upgrade"
            )

    def test_current_heads_with_dependency_conflict(
        self, script_directory
    ):
        # a1 is an ancestor of b2 via a depends_on edge; both rows
        # cannot be present at once
        with pytest.raises(
            CommandError, match="not a valid set of heads.*'a1'.*'b2'"
        ):
            plan_migrations(
                script_directory, {"a1", "b2"}, "heads", "upgrade"
            )

    def test_current_heads_with_only_dependency_consumed(
        self, script_directory
    ):
        # {b2} alone is a legitimate state: applying b2 merges away
        # the a1 row it depends on
        plan = plan_migrations(script_directory, {"b2"}, "heads", "upgrade")
        assert plan.initial_heads == ("b2",)
        assert plan.final_heads == ("m1",)

    def test_upgrade_to_disjoint_branch_fills_from_base(
        self, script_directory
    ):
        # matches the real upgrade command, which plans from base when
        # the target is on a disjoint branch (implicit_base=True)
        plan = plan_migrations(script_directory, {"a2"}, "b2", "upgrade")
        assert [step.revision for step in plan.steps] == ["b1", "b2"]
        assert plan.final_heads == ("a2", "b2")

    def test_unreachable_downgrade_target(self, script_directory):
        with pytest.raises(
            CommandError, match="not a valid downgrade target"
        ):
            plan_migrations(script_directory, {"a2"}, "b1", "downgrade")


class TestPlanPurity(TestBase):
    def test_scripts_and_inputs_unchanged(self, script_directory):
        before = _version_table_hashes(script_directory)

        current = {"a2", "b2"}
        current_copy = set(current)
        plan_migrations(script_directory, current, "heads", "upgrade")
        plan_migrations(script_directory, current, "base", "downgrade")
        plan_migrations(script_directory, None, "heads", "upgrade")

        assert current == current_copy
        assert _version_table_hashes(script_directory) == before

    def test_plan_is_json_serializable(self, script_directory):
        plan = plan_migrations(script_directory, None, "heads", "upgrade")
        as_dict = plan.to_dict()
        serialized = json.dumps(as_dict)
        assert json.loads(serialized)["final_heads"] == ["m1"]
        assert [s["revision"] for s in as_dict["steps"]] == [
            "a1",
            "b1",
            "b2",
            "a2",
            "m1",
        ]
        step = as_dict["steps"][0]
        assert step["version_table_change"] == {
            "operation": "insert",
            "inserts": ["a1"],
            "deletes": [],
            "update": None,
        }

    def test_plan_steps_are_plan_step_instances(self, script_directory):
        plan = plan_migrations(script_directory, None, "heads", "upgrade")
        for step in plan.steps:
            assert isinstance(step, PlanStep)
            assert step.is_upgrade is True

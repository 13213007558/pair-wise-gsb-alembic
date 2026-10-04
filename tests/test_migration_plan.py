from __future__ import annotations

import json
from pathlib import Path
import tempfile

from sqlalchemy import create_engine
from sqlalchemy import insert

from alembic.migration import MigrationContext
from alembic.migration import MigrationStep
from alembic.plan import build_migration_plan
from alembic.plan import PlanError
from alembic.plan import PlanVerificationError
from alembic.plan import verify_migration_plan
from alembic.script import ScriptDirectory
from alembic.script.revision import Revision
from alembic.script.revision import RevisionMap
from alembic.testing import eq_
from alembic.testing import expect_raises_message
from alembic.testing.fixtures import TestBase


def diamond_map():
    return RevisionMap(
        lambda: [
            Revision("a", None),
            Revision("b1", "a", branch_labels="one"),
            Revision("b2", "a", branch_labels="two"),
            Revision("m", ("b1", "b2")),
        ]
    )


class MigrationPlanTest(TestBase):
    def test_diamond_upgrade_plan_is_stable_and_serializable(self):
        plan = build_migration_plan(
            diamond_map(),
            [
                {
                    "database": "db",
                    "current_heads": (),
                    "target": "heads",
                    "direction": "upgrade",
                }
            ],
        )

        json.dumps(plan)
        eq_(plan["atomic"], False)
        eq_(
            [(step["revision"], step["heads_before"], step["heads_after"])
             for step in plan["databases"][0]["steps"]],
            [
                ("a", [], ["a"]),
                ("b1", ["a"], ["b1"]),
                ("b2", ["b1"], ["b1", "b2"]),
                ("m", ["b1", "b2"], ["m"]),
            ],
        )

    def test_independent_branch_tie_is_revision_id(self):
        revision_map = RevisionMap(
            lambda: [
                Revision("a", None),
                Revision("z", "a"),
                Revision("m2", "a"),
            ]
        )
        first = build_migration_plan(
            revision_map,
            [
                {
                    "database": "db",
                    "current_heads": ["a"],
                    "target": "heads",
                    "direction": "upgrade",
                }
            ],
        )
        second = build_migration_plan(
            revision_map,
            [
                {
                    "database": "db",
                    "current_heads": ["a"],
                    "target": "heads",
                    "direction": "upgrade",
                }
            ],
        )

        eq_(first, second)
        eq_(
            [step["revision"] for step in first["databases"][0]["steps"]],
            ["m2", "z"],
        )

    def test_branch_directed_downgrade_keeps_other_branch(self):
        plan = build_migration_plan(
            diamond_map(),
            [
                {
                    "database": "db",
                    "current_heads": ["m"],
                    "target": "a",
                    "branch_label": "one",
                    "direction": "downgrade",
                }
            ],
        )
        eq_(
            [(step["revision"], step["heads_before"], step["heads_after"])
             for step in plan["databases"][0]["steps"]],
            [
                ("m", ["m"], ["b1", "b2"]),
                ("b1", ["b1", "b2"], ["b2"]),
            ],
        )

    def test_ambiguous_relative_downgrade_is_rejected(self):
        with expect_raises_message(
            PlanError, "ambiguous.*specify a branch label"
        ):
            build_migration_plan(
                diamond_map(),
                [
                    {
                        "database": "db",
                        "current_heads": ["b1", "b2"],
                        "target": "-1",
                        "direction": "downgrade",
                    }
                ],
            )

    def test_broken_graph_is_rejected(self):
        revision_map = RevisionMap(
            lambda: [Revision("a", None), Revision("b", "missing")]
        )
        with expect_raises_message(
            PlanError,
            "Broken migration graph: b references missing revision missing",
        ):
            build_migration_plan(revision_map, [])

    def test_unreachable_upgrade_is_rejected(self):
        revision_map = RevisionMap(
            lambda: [
                Revision("a", None),
                Revision("b", "a"),
                Revision("c", None),
            ]
        )
        with expect_raises_message(PlanError, "outside the target ancestry"):
            build_migration_plan(
                revision_map,
                [
                    {
                        "database": "db",
                        "current_heads": ["c"],
                        "target": "b",
                        "direction": "upgrade",
                    }
                ],
            )

    def test_unknown_current_head_is_rejected(self):
        with expect_raises_message(PlanError, "not in the migration graph"):
            build_migration_plan(
                diamond_map(),
                [
                    {
                        "database": "db",
                        "current_heads": ["missing"],
                        "target": "heads",
                        "direction": "upgrade",
                    }
                ],
            )

    def test_depends_on_reason_and_relative_target(self):
        revision_map = RevisionMap(
            lambda: [
                Revision("a", None),
                Revision("b", None),
                Revision("c", "b", dependencies="a"),
            ]
        )
        plan = build_migration_plan(
            revision_map,
            [
                {
                    "database": "db",
                    "current_heads": ["b"],
                    "target": "+1",
                    "direction": "upgrade",
                }
            ],
        )
        eq_(
            [
                (
                    step["revision"],
                    step["heads_before"],
                    step["heads_after"],
                )
                for step in plan["databases"][0]["steps"]
            ],
            [("a", ["b"], ["a", "b"]), ("c", ["a", "b"], ["a", "c"])],
        )
        eq_(
            plan["databases"][0]["steps"][0]["reasons"],
            [
                {
                    "kind": "depends_on",
                    "revision": "c",
                    "references": "a",
                }
            ],
        )

    def test_depends_on_upgrade_preserves_dependency_heads(self):
        revision_map = RevisionMap(
            lambda: [
                Revision("a1", None),
                Revision("a2", None),
                Revision("d1", "a1", dependencies="a2"),
                Revision("a3", "a2"),
            ]
        )
        plan = build_migration_plan(
            revision_map,
            [
                {
                    "database": "db",
                    "current_heads": ["a1", "a2"],
                    "target": "heads",
                    "direction": "upgrade",
                }
            ],
        )
        eq_(
            [
                (step["revision"], step["heads_before"],
                 step["heads_after"])
                for step in plan["databases"][0]["steps"]
            ],
            [
                ("a3", ["a1", "a2"], ["a1", "a3"]),
                ("d1", ["a1", "a3"], ["a3", "d1"]),
            ],
        )

    def test_verify_rejects_changed_heads_and_graph(self):
        revision_map = diamond_map()
        plan = build_migration_plan(
            revision_map,
            [
                {
                    "database": "db",
                    "current_heads": ["a"],
                    "target": "heads",
                    "direction": "upgrade",
                }
            ],
        )
        with expect_raises_message(
            PlanVerificationError, "Current heads.*do not match"
        ):
            verify_migration_plan(
                plan, {"db": ["b1"]}, revision_map
            )

        changed_graph = RevisionMap(
            lambda: [
                Revision("a", None),
                Revision("b1", "a"),
                Revision("b2", "a"),
                Revision("m", ("b1", "b2")),
                Revision("x", "m"),
            ]
        )
        with expect_raises_message(
            PlanVerificationError, "Migration graph identity mismatch"
        ):
            verify_migration_plan(plan, {"db": ["a"]}, changed_graph)


class MigrationPlanExecutionTest(TestBase):
    __only_on__ = "sqlite"

    def _write_script(
        self, path: Path, revision_id: str, down_revision,
        branch_labels=None,
    ) -> None:
        labels = "None" if branch_labels is None else repr(branch_labels)
        path.write_text(
            f"""\
revision = {revision_id!r}
down_revision = {down_revision!r}
branch_labels = {labels}


def upgrade():
    pass


def downgrade():
    pass
"""
        )

    def test_sqlite_executor_matches_planned_version_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            versions_one = temp_path / "one"
            versions_two = temp_path / "two"
            versions_one.mkdir()
            versions_two.mkdir()
            (temp_path / "script.py.mako").write_text("")

            self._write_script(versions_one / "a.py", "a", None)
            self._write_script(
                versions_one / "b1.py", "b1", "a", "one"
            )
            self._write_script(
                versions_two / "b2.py", "b2", "a", "two"
            )
            self._write_script(
                versions_two / "m.py", "m", ("b1", "b2")
            )

            script = ScriptDirectory(
                temp_path,
                version_locations=[versions_one, versions_two],
            )
            graph_identity = script.revision_graph_identity()
            upgrade_db = f"sqlite:///{temp_path / 'upgrade.db'}"
            downgrade_db = f"sqlite:///{temp_path / 'downgrade.db'}"
            upgrade_engine = create_engine(upgrade_db)
            downgrade_engine = create_engine(downgrade_db)

            plan = script.build_migration_plan(
                [
                    {
                        "database": "upgrade",
                        "current_heads": [],
                        "target": "heads",
                        "direction": "upgrade",
                    },
                    {
                        "database": "downgrade",
                        "current_heads": ["m"],
                        "target": "a",
                        "branch_label": "one",
                        "direction": "downgrade",
                    },
                ]
            )
            eq_(plan["atomic"], False)

            self._stamp_initial_heads(
                downgrade_engine, ["m"]
            )

            self._execute_plan(plan, script, {
                "upgrade": upgrade_engine,
                "downgrade": downgrade_engine,
            })
            upgrade_engine.dispose()
            downgrade_engine.dispose()

            (versions_two / "m.py").write_text(
                """\
revision = 'm'
down_revision = ('b1', 'b2')


def upgrade():
    raise RuntimeError("migration body is not graph identity")


def downgrade():
    raise RuntimeError("migration body is not graph identity")
"""
            )
            body_only_script = ScriptDirectory(
                temp_path,
                version_locations=[versions_one, versions_two],
            )
            eq_(
                body_only_script.revision_graph_identity(),
                graph_identity,
            )

    def _stamp_initial_heads(self, engine, heads):
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            context._ensure_version_table()
            connection.execute(
                insert(context._version).values(
                    [{"version_num": head} for head in heads]
                )
            )

    def _execute_plan(self, plan, script, engines):
        current_heads = {}
        for name, current_engine in engines.items():
            with current_engine.connect() as connection:
                current_heads[name] = list(
                    MigrationContext.configure(connection).get_current_heads()
                )
        verify_migration_plan(plan, current_heads, script.revision_map)

        for database_plan in plan["databases"]:
            engine = engines[database_plan["database"]]
            for step in database_plan["steps"]:
                revision = script.get_revision(step["revision"])
                migration_step = (
                    MigrationStep.upgrade_from_script(
                        script.revision_map, revision
                    )
                    if step["operation"] == "upgrade"
                    else MigrationStep.downgrade_from_script(
                        script.revision_map, revision
                    )
                )
                with engine.begin() as connection:
                    context = MigrationContext.configure(
                        connection,
                        opts={
                            "fn": lambda heads, ctx: [migration_step]
                        },
                    )
                    actual_before = set(context.get_current_heads())
                    eq_(set(step["heads_before"]), actual_before)

                    context.run_migrations()

                    actual_after = set(context.get_current_heads())
                    eq_(set(step["heads_after"]), actual_after)

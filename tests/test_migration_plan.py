from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.runtime.migration import RevisionStep
from alembic.script import MigrationPlanError
from alembic.script import ScriptDirectory
from alembic.script.plan import create_migration_plan
from alembic.script.plan import check_migration_plan
from alembic.script.revision import Revision
from alembic.script.revision import RevisionMap


def _write_revision(path: Path, revision_id: str, **attributes) -> None:
    lines = [
        f"revision = {revision_id!r}",
    ]
    for name in (
        "down_revision",
        "depends_on",
        "branch_labels",
    ):
        if name in attributes:
            lines.append(f"{name} = {attributes[name]!r}")
    lines.extend(
        [
            "",
            "def upgrade():",
            "    return None",
            "",
            "def downgrade():",
            "    return None",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def _script_directory(root: Path) -> ScriptDirectory:
    core = root / "core"
    extra = root / "extra"
    core.mkdir()
    extra.mkdir()

    _write_revision(core / "a.py", "a", down_revision=None)
    _write_revision(core / "b.py", "b", down_revision="a")
    _write_revision(
        core / "c1.py",
        "c1",
        down_revision="b",
        branch_labels="br1",
    )
    _write_revision(extra / "d1.py", "d1", down_revision="c1")
    _write_revision(
        extra / "c2.py",
        "c2",
        down_revision="b",
        branch_labels="br2",
    )
    _write_revision(
        core / "d2.py",
        "d2",
        down_revision="c2",
        depends_on="c1",
    )
    _write_revision(
        extra / "m.py",
        "m",
        down_revision=("d1", "d2"),
    )

    config = Config()
    config.set_main_option("script_location", str(root))
    config.set_main_option("version_locations", f"{core} {extra}")
    config.set_main_option("path_separator", "space")
    return ScriptDirectory.from_config(config)


def _heads(engine, table="alembic_version"):
    if table not in inspect(engine).get_table_names():
        return ()
    with engine.connect() as connection:
        rows = connection.execute(
            text(f"select version_num from {table}")
        ).scalars().all()
    return tuple(sorted(rows))


def _run_step(engine, script_directory, step):
    script = script_directory.get_revision(step.revision)

    def migrations_fn(heads, context):
        return [
            RevisionStep(
                script_directory.revision_map,
                script,
                step.operation == "upgrade",
            )
        ]

    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"fn": migrations_fn}
        )
        context._ensure_version_table()
        context.run_migrations()
        connection.commit()


def test_plan_is_serializable_and_stable(tmp_path):
    script = _script_directory(tmp_path)
    requests = [
        {
            "database": "second",
            "current_heads": ("d1", "d2"),
            "target_revision": "br2@b",
            "direction": "downgrade",
        },
        {
            "database": "first",
            "current_heads": (),
            "target_revision": "heads",
            "direction": "upgrade",
        },
    ]

    first = script.create_migration_plan(requests).to_dict()
    second = script.create_migration_plan(requests).to_dict()

    assert first == second
    assert first["databases"][0]["database"] == "second"
    assert [
        step["revision"]
        for step in first["databases"][1]["steps"]
    ] == ["a", "b", "c1", "c2", "d1", "d2", "m"]


def test_planned_steps_match_sqlite_version_table(tmp_path):
    script = _script_directory(tmp_path)
    alpha = create_engine("sqlite://")
    beta = create_engine("sqlite://")

    plan = script.create_migration_plan(
        [
            {
                "database": "alpha",
                "current_heads": (),
                "target_revision": "m",
                "direction": "upgrade",
            },
            {
                "database": "beta",
                "current_heads": ("m",),
                "target_revision": "br2@c2",
                "direction": "downgrade",
            },
        ]
    )

    check_migration_plan(
        script,
        plan.to_dict(),
        {"alpha": (), "beta": ("m",)},
    )

    alpha_steps = plan.databases[0].steps
    beta_steps = plan.databases[1].steps
    assert [step.revision for step in beta_steps] == ["m", "d2"]
    assert _heads(alpha) == ()
    for step in alpha_steps:
        assert _heads(alpha) == step.heads_before
        _run_step(alpha, script, step)
        assert _heads(alpha) == step.heads_after

    assert _heads(beta) == ()
    with beta.connect() as connection:
        MigrationContext.configure(connection)._ensure_version_table()
        connection.exec_driver_sql("insert into alembic_version values ('m')")
        connection.commit()

    for step in beta_steps:
        assert _heads(beta) == step.heads_before
        _run_step(beta, script, step)
        assert _heads(beta) == step.heads_after


def test_graph_identity_ignores_body_but_rejects_edge_change(tmp_path):
    script = _script_directory(tmp_path)
    plan = script.create_migration_plan(
        [
            {
                "database": "db",
                "current_heads": (),
                "target_revision": "heads",
                "direction": "upgrade",
            }
        ]
    )
    d2 = tmp_path / "core" / "d2.py"
    d2.write_text(d2.read_text() + "\n# body-only migration script change\n")

    check_migration_plan(
        script,
        plan.to_dict(),
        {"db": ()},
    )

    d2.write_text(
        d2.read_text().replace(
            "depends_on = 'c1'", "depends_on = 'c2'"
        )
    )
    changed_script = ScriptDirectory(
        tmp_path,
        version_locations=[tmp_path / "core", tmp_path / "extra"],
    )
    try:
        check_migration_plan(
            changed_script,
            plan.to_dict(),
            {"db": ()},
        )
    except MigrationPlanError as exc:
        assert "Migration graph identity" in str(exc)
    else:
        raise AssertionError("Changed migration graph was accepted")


def test_ambiguous_relative_downgrade_is_rejected():
    revisions = [
        Revision("a", None),
        Revision("b", "a"),
        Revision("c1", "b"),
        Revision("c2", "b"),
    ]
    revmap = RevisionMap(lambda: iter(revisions))

    try:
        create_migration_plan(
            revmap,
            [
                {
                    "database": "db",
                    "current_heads": ("c1", "c2"),
                    "target_revision": "-1",
                    "direction": "downgrade",
                }
            ],
        )
    except MigrationPlanError as exc:
        assert "ambiguous" in str(exc)
    else:
        raise AssertionError("Ambiguous downgrade was accepted")


def test_broken_graph_and_unreachable_target_are_rejected():
    broken = RevisionMap(
        lambda: [
            Revision("a", None),
            Revision("b", "missing"),
        ]
    )
    try:
        create_migration_plan(
            broken,
            [
                {
                    "database": "db",
                    "current_heads": (),
                    "target_revision": "heads",
                    "direction": "upgrade",
                }
            ],
        )
    except MigrationPlanError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("Broken graph was accepted")

    unreachable = RevisionMap(
        lambda: [
            Revision("a1", None),
            Revision("b1", "a1"),
            Revision("c1", "b1"),
            Revision("a2", None),
            Revision("b2", "a2"),
            Revision("c2", "b2", dependencies="c1"),
            Revision("d1", "c1"),
        ]
    )
    try:
        create_migration_plan(
            unreachable,
            [
                {
                    "database": "db",
                    "current_heads": ("d1", "c2"),
                    "target_revision": "a1",
                    "direction": "downgrade",
                }
            ],
        )
    except MigrationPlanError as exc:
        assert "Cannot downgrade" in str(exc)
    else:
        raise AssertionError("Unreachable target was accepted")

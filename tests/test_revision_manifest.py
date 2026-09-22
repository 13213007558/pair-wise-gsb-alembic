from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from alembic import command
from alembic import util
from alembic.script import ScriptDirectory
from alembic.testing import expect_raises_message
from alembic.testing.env import _get_staging_directory
from alembic.testing.env import make_sourceless
from alembic.testing.env import _multi_dir_testing_config
from alembic.testing.env import _sqlite_testing_config
from alembic.testing.env import clear_staging_env
from alembic.testing.env import env_file_fixture
from alembic.testing.env import staging_env
from alembic.testing.fixtures import TestBase


def _write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _revision_content(rev: str, down_revision: str | None) -> bytes:
    parent = "None" if down_revision is None else repr(down_revision)
    return (
        f"revision = {rev!r}\n"
        f"down_revision = {parent}\n"

        "def upgrade():\n"
        "    pass\n"

        "def downgrade():\n"
        "    pass\n"
    ).encode()


class RevisionManifestTest(TestBase):
    def setUp(self):
        staging_env()
        self.cfg = _multi_dir_testing_config()
        self.script_dir = Path(_get_staging_directory(), "scripts")
        self.manifest_path = Path(
            _get_staging_directory(), "revision-manifest.json"
        )
        self.cfg.set_main_option(
            "revision_manifest", str(self.manifest_path)
        )

        self.a_path = Path(
            _get_staging_directory(), "model1", "a_revision.py"
        )
        self.b_path = Path(
            _get_staging_directory(), "model2", "b_revision.py"
        )
        self.model3_dir = Path(_get_staging_directory(), "model3")

        self.a_content = _revision_content("a", None)
        self.b_content = _revision_content("b", "a")
        _write_file(self.a_path, self.a_content)
        _write_file(self.b_path, self.b_content)

    def tearDown(self):
        clear_staging_env()

    def _entries(self):
        manifest_dir = self.manifest_path.parent
        return {
            os.path.relpath(path, manifest_dir): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in (self.a_path, self.b_path)
        }

    def _write_manifest(self, entries=None):
        entries = self._entries() if entries is None else entries
        self.manifest_path.write_text(
            json.dumps({"revision_files": entries})
        )

    def test_strict_mode_loads_multiple_version_locations(self):
        self._write_manifest()
        script = ScriptDirectory.from_config(self.cfg)

        eq_revisions = [rev.revision for rev in script.walk_revisions()]
        assert eq_revisions == ["b", "a"]

    def test_unenabled_mode_loads_without_manifest(self):
        self.cfg.remove_main_option("revision_manifest")
        script = ScriptDirectory.from_config(self.cfg)

        assert {rev.revision for rev in script.walk_revisions()} == {
            "a",
            "b",
        }

    def test_missing_file_is_rejected_before_graph_load(self):
        self._write_manifest()
        self.a_path.unlink()

        with expect_raises_message(
            util.CommandError, "Missing revision file"
        ):
            ScriptDirectory.from_config(self.cfg).get_revisions("heads")

    def test_extra_file_is_rejected(self):
        self._write_manifest()
        extra_path = Path(
            _get_staging_directory(), "model2", "extra_revision.py"
        )
        _write_file(extra_path, _revision_content("c", "b"))

        with expect_raises_message(
            util.CommandError, "Unexpected revision file"
        ):
            ScriptDirectory.from_config(self.cfg).get_revisions("heads")

    def test_modified_file_is_rejected_before_import(self):
        marker_path = Path(_get_staging_directory(), "tampered.marker")
        tampered = self.a_content + (
            f"\nopen({str(marker_path)!r}, 'w').write('tampered')\n"
        ).encode()
        self._write_manifest()
        self.a_path.write_bytes(tampered)

        with expect_raises_message(
            util.CommandError,
            "SHA-256 mismatch for revision file",
        ):
            ScriptDirectory.from_config(self.cfg).get_revisions("heads")
        assert not marker_path.exists()

    def test_upgrade_rejects_modified_file_before_environment(self):
        marker_path = Path(_get_staging_directory(), "tampered.marker")
        tampered = self.a_content + (
            f"\nopen({str(marker_path)!r}, 'w').write('tampered')\n"
        ).encode()
        self._write_manifest()
        self.a_path.write_bytes(tampered)

        with expect_raises_message(
            util.CommandError,
            "SHA-256 mismatch for revision file",
        ):
            command.upgrade(self.cfg, "heads")
        assert not marker_path.exists()

    def test_downgrade_rejects_modified_file_before_environment(self):
        marker_path = Path(_get_staging_directory(), "tampered.marker")
        tampered = self.a_content + (
            f"\nopen({str(marker_path)!r}, 'w').write('tampered')\n"
        ).encode()
        self._write_manifest()
        self.a_path.write_bytes(tampered)

        with expect_raises_message(
            util.CommandError,
            "SHA-256 mismatch for revision file",
        ):
            command.downgrade(self.cfg, "base")
        assert not marker_path.exists()

    def test_path_outside_version_locations_is_rejected(self):
        outside_path = Path(_get_staging_directory(), "outside.py")
        _write_file(outside_path, self.a_content)
        manifest_dir = self.manifest_path.parent
        self._write_manifest(
            {
                os.path.relpath(outside_path, manifest_dir): hashlib.sha256(
                    self.a_content
                ).hexdigest()
            }
        )

        with expect_raises_message(
            util.CommandError,
            "Manifest path is outside declared version_locations",
        ):
            ScriptDirectory.from_config(self.cfg).get_revisions("heads")

    def test_invalid_manifest_schema_is_rejected(self):
        self.manifest_path.write_text(
            json.dumps(
                {
                    "revision_files": [
                        (
                            os.path.relpath(
                                self.a_path, self.manifest_path.parent
                            ),
                            "a",
                        ),
                        (
                            os.path.relpath(
                                self.a_path, self.manifest_path.parent
                            ),
                            "b",
                        ),
                    ]
                }
            )
        )

        with expect_raises_message(
            util.CommandError, "must contain a 'revision_files' object"
        ):
            ScriptDirectory.from_config(self.cfg).get_revisions("heads")

    def test_duplicate_json_key_is_rejected(self):
        rel_path = os.path.relpath(self.a_path, self.manifest_path.parent)
        digest = hashlib.sha256(self.a_content).hexdigest()
        self.manifest_path.write_text(
            '{"revision_files": {'
            f'{json.dumps(rel_path)}: {json.dumps(digest)}, '
            f'{json.dumps(rel_path)}: {json.dumps(digest)}' + "}}"
        )

        with expect_raises_message(
            util.CommandError, "Duplicate path in revision manifest"
        ):
            ScriptDirectory.from_config(self.cfg).get_revisions("heads")

    def test_normalized_duplicate_path_is_rejected(self):
        manifest_dir = self.manifest_path.parent
        rel_path = os.path.relpath(self.a_path, manifest_dir)
        digest = hashlib.sha256(self.a_content).hexdigest()
        same_rel_path = os.path.join(os.path.dirname(rel_path), ".")
        same_rel_path = os.path.join(same_rel_path, os.path.basename(rel_path))
        self.manifest_path.write_text(
            json.dumps(
                {
                    "revision_files": {
                        rel_path: digest,
                        same_rel_path: digest,
                    }
                }
            )
        )

        with expect_raises_message(
            util.CommandError, "Duplicate path in revision manifest"
        ):
            ScriptDirectory.from_config(self.cfg).get_revisions("heads")

    def test_directory_symlink_alias_is_rejected(self):
        self.model3_dir.symlink_to(self.a_path.parent.resolve())
        a_alias_path = self.model3_dir / self.a_path.name
        manifest_dir = self.manifest_path.parent
        self._write_manifest(
            {
                os.path.relpath(path, manifest_dir): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in (self.a_path, a_alias_path, self.b_path)
            }
        )

        with expect_raises_message(
            util.CommandError, "symbolic link alias"
        ):
            ScriptDirectory.from_config(self.cfg).get_revisions("heads")
        assert self.model3_dir.is_symlink()


class RevisionManifestSemanticsTest(TestBase):
    def setUp(self):
        staging_env()
        self.cfg = _sqlite_testing_config()
        self.script_dir = Path(_get_staging_directory(), "scripts")
        self.manifest_path = Path(
            _get_staging_directory(), "revision-manifest.json"
        )
        self.cfg.set_main_option(
            "revision_manifest", str(self.manifest_path)
        )

        env_file_fixture(
            """
from sqlalchemy import engine_from_config, pool

def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix='sqlalchemy.', poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()

run_migrations_online()
"""
        )

    def tearDown(self):
        clear_staging_env()

    def test_verified_module_preserves_file_path(self):
        revision_path = self.script_dir / "versions" / "a_revision.py"
        revision_path.write_bytes(
            (
                "revision = 'a'\n"
                "down_revision = None\n"
                "expected_file = __file__\n"
                "def upgrade():\n"
                "    pass\n"
                "def downgrade():\n"
                "    pass\n"
            ).encode()
        )
        self._write_manifest([revision_path])

        script = ScriptDirectory.from_config(self.cfg).get_revision("a")
        assert Path(script.module.__file__) == revision_path.resolve()
        assert Path(script.module.expected_file) == revision_path.resolve()

    def test_verified_module_preserves_source_encoding(self):
        revision_path = self.script_dir / "versions" / "a_revision.py"
        revision_path.write_bytes(
            (
                "# coding: utf-8\n"
                "revision = 'a'\n"
                "down_revision = None\n"
                "message = 'café'\n"
                "def upgrade():\n"
                "    pass\n"
                "def downgrade():\n"
                "    pass\n"
            ).encode("utf-8")
        )
        self._write_manifest([revision_path])

        script = ScriptDirectory.from_config(self.cfg).get_revision("a")
        assert script.module.message == "café"

    def test_verified_revision_can_upgrade_and_downgrade(self):
        revision_path = self.script_dir / "versions" / "a_revision.py"
        revision_path.write_bytes(_revision_content("a", None))
        self._write_manifest([revision_path])

        command.upgrade(self.cfg, "a")
        command.downgrade(self.cfg, "base")

    def test_verified_sourceless_revision_loads_verified_bytes(self):
        revision_path = self.script_dir / "versions" / "a_revision.py"
        revision_path.write_bytes(_revision_content("a", None))
        make_sourceless(revision_path, "simple")
        pyc_path = revision_path.with_suffix(".pyc")
        self._write_manifest([pyc_path])

        self.cfg.set_main_option("sourceless", "true")
        script = ScriptDirectory.from_config(self.cfg)
        assert script.get_revision("a").module.revision == "a"

    def test_replacement_after_verification_executes_verified_bytes(self):
        revision_path = self.script_dir / "versions" / "a_revision.py"
        original = _revision_content("a", None)
        revision_path.write_bytes(original)
        self._write_manifest([revision_path])
        script_directory = ScriptDirectory.from_config(self.cfg)

        script_directory._verify_revision_files(
            script_directory._version_locations
        )
        revision_path.write_bytes(
            original + b"\nreplacement_loaded = True\n"
        )
        revisions = list(script_directory.walk_revisions())
        assert not hasattr(revisions[0].module, "replacement_loaded")

    def _write_manifest(self, paths):
        self.manifest_path.write_text(
            json.dumps(
                {
                    "revision_files": {
                        os.path.relpath(path, self.manifest_path.parent): (
                            hashlib.sha256(path.read_bytes()).hexdigest()
                        )
                        for path in paths
                    }
                }
            )
        )

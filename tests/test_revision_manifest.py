from __future__ import annotations

import hashlib
import os
from pathlib import Path

from alembic import command
from alembic import util
from alembic.script import ScriptDirectory
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing.env import _get_staging_directory
from alembic.testing.env import _sqlite_testing_config
from alembic.testing.env import clear_staging_env
from alembic.testing.env import staging_env
from alembic.testing.env import three_rev_fixture
from alembic.testing.fixtures import TestBase
from alembic.util import load_module_py_verified


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RevisionManifestTest(TestBase):
    def setUp(self):
        self.env = staging_env()
        self.cfg = _sqlite_testing_config()
        self.revs = list(three_rev_fixture(self.cfg))
        self.scripts_dir = Path(_get_staging_directory(), "scripts")
        self.versions_dir = self.scripts_dir / "versions"
        self.manifest_path = self.scripts_dir / "revision_manifest.txt"
        self.marker = (
            Path(_get_staging_directory(), "scripts").absolute()
            / "tamper_marker.txt"
        )

    def tearDown(self):
        clear_staging_env()

    def _revision_files(self):
        return sorted(self.versions_dir.glob("*.py"))

    def _write_manifest(self, files=None, extra_lines=()):
        if files is None:
            files = self._revision_files()
        lines = ["# revision manifest"]
        for file_ in files:
            rel = Path(file_).relative_to(self.manifest_path.parent)
            lines.append(f"{_sha256(file_)}  {rel.as_posix()}")
        lines.extend(extra_lines)
        self.manifest_path.write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        return self.manifest_path

    def _enable_manifest(self):
        self.cfg.set_main_option(
            "revision_manifest", str(self.manifest_path)
        )

    def _tamper_revision(self, path):
        path = Path(path)
        path.write_text(
            path.read_text(encoding="utf-8")
            + f"\nopen({str(self.marker)!r}, 'w').write('tampered')\n",
            encoding="utf-8",
        )

    def test_upgrade_downgrade_with_valid_manifest(self):
        self._write_manifest()
        self._enable_manifest()
        command.upgrade(self.cfg, "head", sql=True)
        command.downgrade(self.cfg, "head:base", sql=True)

    def test_revision_graph_with_valid_manifest(self):
        self._write_manifest()
        self._enable_manifest()
        script = ScriptDirectory.from_config(self.cfg)
        eq_(script.get_current_head(), self.revs[-1])
        revs = {scr.revision: scr for scr in script.walk_revisions()}
        eq_(set(revs), set(self.revs))
        scr = revs[self.revs[0]]
        # __file__ of the loaded module still points at the real file
        eq_(
            Path(scr.module.__file__).resolve(),
            Path(scr.path).resolve(),
        )

    def test_tampered_revision_aborts_and_is_not_executed(self):
        self._write_manifest()
        victim = self._revision_files()[1]
        self._tamper_revision(victim)
        self._enable_manifest()

        for callable_ in (
            lambda: command.upgrade(self.cfg, "head", sql=True),
            lambda: command.downgrade(self.cfg, "head:base", sql=True),
            lambda: list(
                ScriptDirectory.from_config(self.cfg).walk_revisions()
            ),
        ):
            assert_raises_message(
                util.CommandError,
                f"SHA-256 mismatch for revision files: .*{victim.name}",
                callable_,
            )
            # the tampered module top level must not have run
            eq_(self.marker.exists(), False)

    def test_tampered_revision_loads_when_manifest_not_configured(self):
        victim = self._revision_files()[1]
        self._tamper_revision(victim)
        script = ScriptDirectory.from_config(self.cfg)
        eq_(len(list(script.walk_revisions())), 3)
        eq_(self.marker.exists(), True)

    def test_missing_revision_file_aborts(self):
        self._write_manifest()
        victim = self._revision_files()[0]
        victim.unlink()
        self._enable_manifest()
        assert_raises_message(
            util.CommandError,
            f"files listed in the manifest are missing: .*{victim.name}",
            lambda: list(
                ScriptDirectory.from_config(self.cfg).walk_revisions()
            ),
        )

    def test_extra_revision_file_aborts(self):
        self._write_manifest()
        extra = self.versions_dir / "zz99_extra.py"
        extra.write_text(
            "revision = 'zz99extra'\ndown_revision = None\n",
            encoding="utf-8",
        )
        self._enable_manifest()
        assert_raises_message(
            util.CommandError,
            f"not listed in the manifest are present: .*{extra.name}",
            lambda: list(
                ScriptDirectory.from_config(self.cfg).walk_revisions()
            ),
        )

    def test_duplicate_manifest_path_rejected(self):
        victim = self._revision_files()[0]
        rel = victim.relative_to(self.manifest_path.parent)
        self._write_manifest(
            extra_lines=[f"{_sha256(victim)}  {rel.as_posix()}"]
        )
        self._enable_manifest()
        assert_raises_message(
            util.CommandError,
            f"duplicate path '{rel.as_posix()}'",
            lambda: list(
                ScriptDirectory.from_config(self.cfg).walk_revisions()
            ),
        )

    def test_manifest_path_escaping_manifest_dir_rejected(self):
        self._write_manifest(extra_lines=[f"{'0' * 64}  ../outside.py"])
        self._enable_manifest()
        assert_raises_message(
            util.CommandError,
            "escapes the manifest directory",
            lambda: list(
                ScriptDirectory.from_config(self.cfg).walk_revisions()
            ),
        )

    def test_absolute_manifest_path_rejected(self):
        self._write_manifest(extra_lines=[f"{'0' * 64}  /etc/passwd"])
        self._enable_manifest()
        assert_raises_message(
            util.CommandError,
            "absolute path '/etc/passwd' is not allowed",
            lambda: list(
                ScriptDirectory.from_config(self.cfg).walk_revisions()
            ),
        )

    def test_manifest_path_outside_version_locations_rejected(self):
        # env.py is inside the manifest directory, but not inside any
        # declared version location
        env_py = self.scripts_dir / "env.py"
        self._write_manifest(extra_lines=[f"{_sha256(env_py)}  env.py"])
        self._enable_manifest()
        assert_raises_message(
            util.CommandError,
            "outside the declared version locations",
            lambda: list(
                ScriptDirectory.from_config(self.cfg).walk_revisions()
            ),
        )

    def test_symlink_alias_rejected(self):
        files = self._revision_files()
        victim = files[0]
        alias = self.versions_dir / f"alias_{victim.name}"
        os.symlink(victim.name, alias)
        rel = alias.relative_to(self.manifest_path.parent)
        self._write_manifest(
            files=files,
            extra_lines=[f"{_sha256(victim)}  {rel.as_posix()}"],
        )
        self._enable_manifest()
        assert_raises_message(
            util.CommandError,
            "symbolic link aliases of the same file are not allowed",
            lambda: list(
                ScriptDirectory.from_config(self.cfg).walk_revisions()
            ),
        )

    def test_multiple_version_locations(self):
        other_dir = self.scripts_dir / "other_versions"
        other_dir.mkdir()
        moved = self._revision_files()[-1]
        moved.rename(other_dir / moved.name)
        self.cfg.set_main_option("path_separator", "space")
        self.cfg.set_main_option(
            "version_locations", f"{self.versions_dir} {other_dir}"
        )
        files = sorted(self.versions_dir.glob("*.py")) + sorted(
            other_dir.glob("*.py")
        )
        self._write_manifest(files=files)
        self._enable_manifest()
        script = ScriptDirectory.from_config(self.cfg)
        eq_(len(list(script.walk_revisions())), 3)
        eq_(script.get_current_head(), self.revs[-1])

    def test_generate_revision_still_works_with_manifest(self):
        self._write_manifest()
        self._enable_manifest()
        script = ScriptDirectory.from_config(self.cfg)
        new_script = script.generate_revision(
            util.rev_id(), "new rev", refresh=True, head="head"
        )
        assert new_script is not None

    def test_verified_bytes_loader_runs_verified_content(self):
        target = self.scripts_dir / "toctou_module.py"
        target.write_text("VALUE = 'original'\n", encoding="utf-8")
        verified_data = target.read_bytes()
        # replace the file after "verification"; the loader must still
        # execute the verified bytes, not the new content
        target.write_text(
            "VALUE = 'replaced'\n"
            f"open({str(self.marker)!r}, 'w').write('tampered')\n",
            encoding="utf-8",
        )
        module = load_module_py_verified(
            "toctou_module", target, verified_data
        )
        eq_(module.VALUE, "original")
        eq_(Path(module.__file__).resolve(), target.resolve())
        eq_(self.marker.exists(), False)

    def test_verified_bytes_loader_honors_encoding_declaration(self):
        target = self.scripts_dir / "encoding_module.py"
        data = "# -*- coding: latin-1 -*-\nVALUE = 'caf\xe9'\n".encode(
            "latin-1"
        )
        target.write_bytes(data)
        module = load_module_py_verified("encoding_module", target, data)
        eq_(module.VALUE, "caf\xe9")

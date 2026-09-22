"""Verification of revision files against a SHA-256 manifest.

A release package may ship a manifest file listing the revision files
that are allowed to be executed, together with their SHA-256 digests.
When the ``revision_manifest`` option is configured, the files present
in the version locations are checked against this manifest before any
Python migration module is loaded, and only the verified bytes are
executed.

This is an integrity check for trusted release packages; it is not a
Python sandbox and does not involve signatures or key management.

The manifest format is compatible with ``sha256sum`` output: each
non-empty, non-comment line contains a lowercase or uppercase hex
SHA-256 digest, whitespace, and a path relative to the directory
containing the manifest file::

    # revision files for release 2026.09
    9f2c...  versions/20260901_abc123_add_users.py
    51ab...  versions/20260910_def456_add_orders.py

"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Sequence
import hashlib
import os
from pathlib import Path
from pathlib import PurePosixPath
import re

from ..util.exc import CommandError

_manifest_line = re.compile(r"^([0-9a-fA-F]{64})[ \t]+(\S.*)$")
_windows_drive = re.compile(r"^[a-zA-Z]:")


def _normalize_manifest_path(
    rel_path: str, manifest_path: Path, lineno: int
) -> str:
    """Validate and normalize a path entry from the manifest.

    Paths are interpreted relative to the directory containing the
    manifest.  Absolute paths and paths that escape that directory
    via ``..`` elements are rejected.

    """
    if _windows_drive.match(rel_path):
        raise CommandError(
            f"Revision manifest {manifest_path}, line {lineno}: "
            f"absolute path {rel_path!r} is not allowed; paths must be "
            "relative to the manifest file"
        )
    pure = PurePosixPath(rel_path)
    if pure.is_absolute():
        raise CommandError(
            f"Revision manifest {manifest_path}, line {lineno}: "
            f"absolute path {rel_path!r} is not allowed; paths must be "
            "relative to the manifest file"
        )
    if not pure.parts or str(pure) == ".":
        raise CommandError(
            f"Revision manifest {manifest_path}, line {lineno}: "
            f"invalid path {rel_path!r}"
        )
    if ".." in pure.parts:
        raise CommandError(
            f"Revision manifest {manifest_path}, line {lineno}: "
            f"path {rel_path!r} escapes the manifest directory; "
            "'..' elements are not allowed"
        )
    return str(pure)


def _parse_manifest(manifest_path: Path) -> list[tuple[str, str]]:
    """Parse the manifest, returning (relative_path, sha256) entries."""
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as err:
        raise CommandError(
            f"Can't read revision manifest {manifest_path}: {err}"
        ) from err

    entries: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _manifest_line.match(line)
        if match is None:
            raise CommandError(
                f"Revision manifest {manifest_path}, line {lineno}: "
                f"can't parse line {line!r}; expected "
                "'<sha256-hex>  <relative path>'"
            )
        sha256 = match.group(1).lower()
        rel_path = _normalize_manifest_path(
            match.group(2).strip(), manifest_path, lineno
        )
        if rel_path in seen:
            raise CommandError(
                f"Revision manifest {manifest_path}, line {lineno}: "
                f"duplicate path {rel_path!r} (first seen on "
                f"line {seen[rel_path]})"
            )
        seen[rel_path] = lineno
        entries.append((rel_path, sha256))
    return entries


def verify_revision_files(
    manifest_path: str | os.PathLike[str],
    version_locations: Sequence[Path],
    disk_revision_files: Iterable[Path],
) -> dict[Path, bytes]:
    """Verify revision files against a SHA-256 manifest.

    :param manifest_path: path to the manifest file.  Manifest entries
     are interpreted relative to the directory containing this file.
    :param version_locations: the declared version locations; every
     manifest entry must resolve to a file inside one of these
     directories.
    :param disk_revision_files: the revision files actually present in
     the version locations.

    Returns a mapping of resolved file paths to their verified raw
    bytes.  Raises :class:`.CommandError` if any file was modified, is
    missing, or is unexpectedly present, or if the manifest itself is
    invalid (duplicate paths, paths escaping the declared directories,
    or symbolic link aliases of the same file).

    """
    manifest_path = Path(manifest_path)
    entries = _parse_manifest(manifest_path)
    manifest_dir = manifest_path.absolute().parent
    resolved_locations = [Path(loc).resolve() for loc in version_locations]

    resolved_entries: dict[Path, tuple[str, str]] = {}
    for rel_path, sha256 in entries:
        resolved = (manifest_dir / rel_path).resolve()
        if not any(
            resolved.is_relative_to(location)
            for location in resolved_locations
        ):
            raise CommandError(
                f"Revision manifest entry {rel_path!r} resolves to "
                f"{resolved}, which is outside the declared "
                "version locations"
            )
        if resolved in resolved_entries:
            other_rel_path, _ = resolved_entries[resolved]
            raise CommandError(
                f"Revision manifest entries {other_rel_path!r} and "
                f"{rel_path!r} both refer to the file {resolved}; "
                "symbolic link aliases of the same file are not allowed"
            )
        resolved_entries[resolved] = (rel_path, sha256)

    disk_resolved = {Path(path).resolve() for path in disk_revision_files}
    manifest_resolved = set(resolved_entries)

    problems = []
    missing = sorted(str(path) for path in manifest_resolved - disk_resolved)
    if missing:
        problems.append(
            "files listed in the manifest are missing: "
            + ", ".join(missing)
        )
    extra = sorted(str(path) for path in disk_resolved - manifest_resolved)
    if extra:
        problems.append(
            "revision files not listed in the manifest are present: "
            + ", ".join(extra)
        )
    if problems:
        raise CommandError(
            f"Revision manifest {manifest_path} does not match the "
            "version directories: " + "; ".join(problems)
        )

    verified: dict[Path, bytes] = {}
    mismatched = []
    for resolved, (rel_path, sha256) in resolved_entries.items():
        data = resolved.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256:
            mismatched.append(
                f"{resolved} (expected {sha256}, got {actual})"
            )
        else:
            verified[resolved] = data
    if mismatched:
        raise CommandError(
            "SHA-256 mismatch for revision files: " + "; ".join(mismatched)
        )
    return verified

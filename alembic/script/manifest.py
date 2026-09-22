from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from ..util import CommandError


class RevisionManifestError(CommandError):
    """Raised when a revision manifest validation fails."""


@dataclass(frozen=True)
class VerifiedRevisionFile:
    path: Path
    data: bytes


def _manifest_error(messages: Sequence[str]) -> RevisionManifestError:
    return RevisionManifestError(
        "Revision manifest verification failed:\n\t- "
        + "\n\t- ".join(messages)
    )


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RevisionManifestError(
                f"Duplicate path in revision manifest: {key}"
            )
        result[key] = value
    return result


def load_revision_manifest(
    manifest_path: str | os.PathLike[str],
) -> dict[Path, str]:
    path = Path(manifest_path)
    try:
        with path.open("rb") as fp:
            payload = json.load(
                fp, object_pairs_hook=_reject_duplicate_json_keys
            )
    except OSError as exc:
        raise RevisionManifestError(
            f"Can't read revision manifest {path}: {exc.strerror or exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RevisionManifestError(
            f"Invalid JSON in revision manifest {path}: {exc.msg}"
        ) from exc

    if not isinstance(payload, dict):
        raise RevisionManifestError(
            f"Revision manifest {path} must contain a JSON object"
        )

    if payload and all(
        isinstance(entry, str) and isinstance(digest, str)
        for entry, digest in payload.items()
    ):
        files: Any = payload
    else:
        files = payload.get("revision_files")
    if not isinstance(files, dict):
        raise RevisionManifestError(
            f"Revision manifest {path} must contain a 'revision_files' object"
        )

    manifest_dir = path.absolute().parent
    entries: dict[Path, str] = {}
    errors: list[str] = []
    for entry, digest in files.items():
        if not isinstance(entry, str) or not entry:
            errors.append("Revision manifest contains an empty file path")
            continue
        if Path(entry).is_absolute():
            errors.append(
                "Manifest path must be relative: "
                f"{manifest_dir / entry}"
            )
            continue
        if not isinstance(digest, str):
            errors.append(f"Digest for {entry} must be a string")
            continue

        normalized_digest = digest.strip().lower()
        if (
            len(normalized_digest) != 64
            or any(c not in "0123456789abcdef" for c in normalized_digest)
        ):
            errors.append(
                f"Digest for {entry} must be a 64 character SHA-256 "
                "hex digest"
            )
            continue

        resolved_entry = Path(os.path.abspath(manifest_dir / entry))
        if resolved_entry in entries:
            errors.append(f"Duplicate path in revision manifest: {entry}")
            continue
        entries[resolved_entry] = normalized_digest

    if errors:
        raise _manifest_error(errors)
    return entries


def verify_revision_manifest(
    manifest_path: str | os.PathLike[str],
    version_locations: Sequence[Path],
    revision_files: Sequence[tuple[Path, Path]],
) -> dict[Path, VerifiedRevisionFile]:
    """Validate revision paths and hash their bytes before import.

    revision_files contains logical and resolved path pairs. The returned
    mapping is keyed by the logical path and never reads the filesystem again.
    """

    manifest_path = Path(manifest_path).absolute()
    entries = load_revision_manifest(manifest_path)
    errors: list[str] = []

    roots = [Path(os.path.abspath(root)) for root in version_locations]
    resolved_roots = [root.resolve(strict=False) for root in roots]

    entry_roots: dict[Path, int] = {}
    for entry in entries:
        for index, root in enumerate(roots):
            try:
                entry.relative_to(root)
            except ValueError:
                continue
            entry_roots[entry] = index
            break
        else:
            errors.append(
                "Manifest path is outside declared version_locations: "
                f"{entry}"
            )

    candidates = {
        logical.absolute(): (
            resolved,
            logical.absolute() != resolved,
        )
        for logical, resolved in revision_files
    }
    for path in sorted(set(entries) - set(candidates)):
        if not path.exists():
            errors.append(f"Missing revision file: {path}")
        else:
            errors.append(
                f"Manifest path is not a loadable revision file: {path}"
            )
    for path in sorted(set(candidates) - set(entries)):
        errors.append(f"Unexpected revision file: {path}")

    pending_data: dict[Path, bytes] = {}
    files_by_identity: dict[tuple[int, int], tuple[Path, bool]] = {}
    for logical_path in sorted(set(entries).intersection(candidates)):
        resolved_path, path_uses_symlink = candidates[logical_path]
        if logical_path.is_symlink():
            errors.append(
                "Revision file is a symbolic link alias: "
                f"{logical_path}"
            )
            continue
        try:
            stat_result = logical_path.stat()
        except OSError as exc:
            errors.append(
                f"Can't read revision file {logical_path}: "
                f"{exc.strerror or exc}"
            )
            continue
        if not stat.S_ISREG(stat_result.st_mode):
            errors.append(
                f"Revision path is not a regular file: {logical_path}"
            )
            continue

        identity = (stat_result.st_dev, stat_result.st_ino)
        if identity in files_by_identity:
            other_path, other_uses_symlink = files_by_identity[identity]
            if path_uses_symlink or other_uses_symlink:
                errors.append(
                    "Revision file is a symbolic link alias of "
                    f"{other_path}: {logical_path}"
                )
                continue
        files_by_identity[identity] = (
            logical_path,
            path_uses_symlink,
        )

        root_index = entry_roots.get(logical_path)
        if root_index is not None:
            try:
                resolved_path.relative_to(resolved_roots[root_index])
            except ValueError:
                errors.append(
                    "Manifest path resolves outside its declared version "
                    f"location: {logical_path}"
                )
                continue

        try:
            data = logical_path.read_bytes()
        except OSError as exc:
            errors.append(
                f"Can't read revision file {logical_path}: "
                f"{exc.strerror or exc}"
            )
            continue

        pending_data[logical_path] = data

    verified: dict[Path, VerifiedRevisionFile] = {}
    for logical_path, data in pending_data.items():
        resolved_path = candidates[logical_path][0]
        digest = hashlib.sha256(data).hexdigest()
        if digest != entries[logical_path]:
            errors.append(
                "SHA-256 mismatch for revision file: "
                f"{logical_path} (expected {entries[logical_path]}, "
                f"got {digest})"
            )
            continue
        verified[logical_path] = VerifiedRevisionFile(resolved_path, data)

    if errors:
        raise _manifest_error(errors)
    return verified

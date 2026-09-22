.. change::
    :tags: usecase, general

    Added optional strict revision integrity verification via the
    ``revision_manifest`` configuration option. The JSON manifest lists
    allowed revision files and their SHA-256 digests. Alembic verifies files
    in all configured version locations before importing revision modules for
    graph reads, upgrades and downgrades, and executes the verified bytes when
    a file is replaced after verification. Manifest paths are relative to the
    manifest file; duplicate, escaped, and symlink-aliased paths are rejected.

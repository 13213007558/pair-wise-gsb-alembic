.. change::
    :tags: feature

    Added optional revision content checksum verification.  When the
    :paramref:`.EnvironmentContext.configure.verify_revision_checksums`
    parameter is enabled, Alembic records a checksum for every revision
    it applies - covering the migration file's raw bytes and its graph
    declarations (``down_revision``, ``branch_labels``,
    ``depends_on``) - and verifies the recorded history before running
    any further ``upgrade()`` / ``downgrade()`` functions, stopping with
    an error that names the revision and file if a script was modified
    after being applied.  Successful downgrades remove the corresponding
    record.  Databases migrated before the feature was enabled can
    establish their initial checksum history explicitly using the new
    :func:`.command.adopt_checksums` command or
    :meth:`.MigrationContext.adopt_checksums`.

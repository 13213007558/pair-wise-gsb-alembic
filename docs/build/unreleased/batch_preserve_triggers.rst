.. change::
    :tags: feature, sqlite

    Added the :paramref:`.Operations.batch_alter_table.preserve_triggers`
    parameter, which allows triggers on a SQLite table to survive a
    "move and copy" table recreate.  Triggers are reflected from the main
    database and re-created with their original definitions after the data
    copy completes, so the copy itself does not fire them.  Triggers that
    depend on columns dropped or renamed by the batch must be given
    complete replacement definitions by the migration author; the batch is
    otherwise aborted before the existing table is modified.  The default
    behavior of silently dropping triggers on recreate is unchanged.

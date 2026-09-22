.. change::
    :tags: feature, batch

    Added the :paramref:`~.Operations.batch_alter_table.copy_values`
    parameter to :meth:`.Operations.batch_alter_table`, which associates
    columns of the new table with SQLAlchemy SQL expressions that are
    evaluated within the "INSERT from SELECT" data copy step of a
    "move and copy" batch migration.  This allows values to be
    transformed in place as the table is rebuilt, e.g. computing a renamed
    or newly added column from the existing columns, such as deriving an
    ``amount`` column from ``cents / 100.0``.  Expressions are resolved
    against the table as it exists before the rebuild, so they may refer
    to columns that are renamed or dropped within the same batch.

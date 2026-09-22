.. change::
    :tags: feature, batch

    Added the
    :paramref:`~.Operations.batch_alter_table.column_expressions` argument
    to :meth:`.Operations.batch_alter_table`, allowing SQLAlchemy
    expressions to be associated with individual target columns so that
    their values are computed within the ``INSERT ... SELECT`` of the
    "move and copy" procedure, e.g. ``amount = cents / 100.0``.
    Expressions refer to the columns of the table as it exists before
    the batch migration, matched by name, and remain valid when those
    columns are renamed or dropped within the same batch.

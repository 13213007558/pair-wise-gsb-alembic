.. change::
    :tags: feature, autogenerate

    Added an optional dependency-aware operation sorting phase for
    autogenerate, enabled with the new
    :paramref:`.EnvironmentContext.configure.sort_ops` parameter or invoked
    directly via :func:`alembic.operations.sort.sort_ops` and
    :func:`alembic.operations.sort.sort_migration_script`.  Operations
    declare which schema-qualified objects they create, read, drop, or
    rename via the new :meth:`.MigrateOperation.op_dependencies` hook;
    operations that declare nothing act as ordering barriers, container
    boundaries such as :class:`.ModifyTableOps` are preserved, and
    dependency cycles raise
    :class:`alembic.operations.sort.OpSortCycleError` naming the objects
    and operations involved.


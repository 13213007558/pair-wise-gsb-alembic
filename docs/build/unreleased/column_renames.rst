.. change::
    :tags: feature, autogenerate

    Added support for explicit column rename mappings in autogenerate via
    the new :paramref:`.EnvironmentContext.configure.column_renames`
    parameter.  The mapping declares renames such as ``customer.full_name``
    to ``customer.display_name`` keyed by schema, table and column identity,
    so that autogenerate renders an ``op.alter_column()`` rename instead
    of a drop/add column pair, while types, nullability, indexes, unique
    constraints and foreign keys that refer to renamed columns continue to
    be compared.  Invalid mappings, including one-to-many renames, targets
    occupied by unrelated columns, cyclic renames and missing sources,
    raise an error before the migration is generated.


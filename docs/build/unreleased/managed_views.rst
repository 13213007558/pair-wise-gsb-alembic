.. change::
    :tags: feature, autogenerate

    Added support for explicitly managed database views.  Views declared
    with :class:`alembic.views.View` against the target metadata -
    including schema, name, defining query and dependent objects -
    participate in autogenerate, operation rendering and downgrade
    generation.  Views are dropped and recreated in dependency order
    around the table operations they depend on, views not declared as
    managed are never touched, and the previous definition is retained
    for downgrades.  Plain views on SQLite are supported initially.


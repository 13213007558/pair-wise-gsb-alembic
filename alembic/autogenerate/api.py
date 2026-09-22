from __future__ import annotations

from collections.abc import Iterator
from collections.abc import Sequence
import contextlib
import logging
from typing import Any
from typing import TYPE_CHECKING

from sqlalchemy import inspect
from sqlalchemy.exc import NoSuchTableError

from . import compare
from . import render
from . import renames as renames_mod
from .renames import ColumnRenameMap
from .. import util
from ..operations import ops
from ..runtime.plugins import Plugin
from ..util import sqla_compat

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.engine import Dialect
    from sqlalchemy.engine import Inspector
    from sqlalchemy.sql.schema import MetaData
    from sqlalchemy.sql.schema import SchemaItem
    from sqlalchemy.sql.schema import Table

    from ..config import Config
    from ..operations.ops import DowngradeOps
    from ..operations.ops import MigrationScript
    from ..operations.ops import UpgradeOps
    from ..runtime.environment import NameFilterParentNames
    from ..runtime.environment import NameFilterType
    from ..runtime.environment import ProcessRevisionDirectiveFn
    from ..runtime.environment import RenderItemFn
    from ..runtime.migration import MigrationContext
    from ..script.base import Script
    from ..script.base import ScriptDirectory
    from ..script.revision import _GetRevArg
    from ..util import PriorityDispatcher


log = logging.getLogger(__name__)


def compare_metadata(
    context: MigrationContext,
    metadata: MetaData,
    column_renames: Any = None,
) -> Any:
    """Compare a database schema to that given in a
    :class:`~sqlalchemy.schema.MetaData` instance.

    The database connection is presented in the context
    of a :class:`.MigrationContext` object, which
    provides database connectivity as well as optional
    comparison functions to use for datatypes and
    server defaults - see the "autogenerate" arguments
    at :meth:`.EnvironmentContext.configure`
    for details on these.

    The return format is a list of "diff" directives,
    each representing individual differences::

        from alembic.migration import MigrationContext
        from alembic.autogenerate import compare_metadata
        from sqlalchemy import (
            create_engine,
            MetaData,
            Column,
            Integer,
            String,
            Table,
            text,
        )
        import pprint

        engine = create_engine("sqlite://")

        with engine.begin() as conn:
            conn.execute(
                text(
                    '''
                        create table foo (
                            id integer not null primary key,
                            old_data varchar,
                            x integer
                        )
                    '''
                )
            )
            conn.execute(text("create table bar (data varchar)"))

        metadata = MetaData()
        Table(
            "foo",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("data", Integer),
            Column("x", Integer, nullable=False),
        )
        Table("bat", metadata, Column("info", String))

        mc = MigrationContext.configure(engine.connect())

        diff = compare_metadata(mc, metadata)
        pprint.pprint(diff, indent=2, width=20)

    Output::

        [
            (
                "add_table",
                Table(
                    "bat",
                    MetaData(),
                    Column("info", String(), table=<bat>),
                    schema=None,
                ),
            ),
            (
                "remove_table",
                Table(
                    "bar",
                    MetaData(),
                    Column("data", VARCHAR(), table=<bar>),
                    schema=None,
                ),
            ),
            (
                "add_column",
                None,
                "foo",
                Column("data", Integer(), table=<foo>),
            ),
            [
                (
                    "modify_nullable",
                    None,
                    "foo",
                    "x",
                    {
                        "existing_comment": None,
                        "existing_server_default": False,
                        "existing_type": INTEGER(),
                    },
                    True,
                    False,
                )
            ],
            (
                "remove_column",
                None,
                "foo",
                Column("old_data", VARCHAR(), table=<foo>),
            ),
        ]

    :param context: a :class:`.MigrationContext`
     instance.
    :param metadata: a :class:`~sqlalchemy.schema.MetaData`
     instance.
    :param column_renames: optional explicit mapping of column renames,
     keyed by (schema, table_name, old_name) tuples with the new column
     name as the value.  See the column_renames parameter of
     EnvironmentContext.configure for details.

    .. seealso::

        :func:`.produce_migrations` - produces a :class:`.MigrationScript`
        structure based on metadata comparison.

    """

    migration_script = produce_migrations(
        context, metadata, column_renames=column_renames
    )
    assert migration_script.upgrade_ops is not None
    return migration_script.upgrade_ops.as_diffs()


def produce_migrations(
    context: MigrationContext,
    metadata: MetaData,
    column_renames: Any = None,
) -> MigrationScript:
    """Produce a :class:`.MigrationScript` structure based on schema
    comparison.

    This function does essentially what :func:`.compare_metadata` does,
    but then runs the resulting list of diffs to produce the full
    :class:`.MigrationScript` object.   For an example of what this looks like,
    see the example in :ref:`customizing_revision`.

    :param context: a :class:`.MigrationContext` instance.
    :param metadata: a :class:`~sqlalchemy.schema.MetaData` instance.
    :param column_renames: optional explicit mapping of column renames;
     see :func:`.compare_metadata` for details.

    .. seealso::

        :func:`.compare_metadata` - returns more fundamental "diff"
        data from comparing a schema.

    """

    opts: dict[str, Any] | None = None
    if column_renames is not None:
        opts = dict(context.opts)
        opts["column_renames"] = column_renames

    autogen_context = AutogenContext(context, metadata=metadata, opts=opts)

    migration_script = ops.MigrationScript(
        rev_id=None,
        upgrade_ops=ops.UpgradeOps([]),
        downgrade_ops=ops.DowngradeOps([]),
    )

    compare._populate_migration_script(autogen_context, migration_script)

    return migration_script


def render_python_code(
    up_or_down_op: UpgradeOps | DowngradeOps,
    sqlalchemy_module_prefix: str = "sa.",
    alembic_module_prefix: str = "op.",
    render_as_batch: bool = False,
    imports: Sequence[str] = (),
    render_item: RenderItemFn | None = None,
    migration_context: MigrationContext | None = None,
    user_module_prefix: str | None = None,
) -> str:
    """Render Python code given an :class:`.UpgradeOps` or
    :class:`.DowngradeOps` object.

    This is a convenience function that can be used to test the
    autogenerate output of a user-defined :class:`.MigrationScript` structure.

    :param up_or_down_op: :class:`.UpgradeOps` or :class:`.DowngradeOps` object
    :param sqlalchemy_module_prefix: module prefix for SQLAlchemy objects
    :param alembic_module_prefix: module prefix for Alembic constructs
    :param render_as_batch: use "batch operations" style for rendering
    :param imports: sequence of import symbols to add
    :param render_item: callable to render items
    :param migration_context: optional :class:`.MigrationContext`
    :param user_module_prefix: optional string prefix for user-defined types

     .. versionadded:: 1.11.0

    """
    opts = {
        "sqlalchemy_module_prefix": sqlalchemy_module_prefix,
        "alembic_module_prefix": alembic_module_prefix,
        "render_item": render_item,
        "render_as_batch": render_as_batch,
        "user_module_prefix": user_module_prefix,
    }

    if migration_context is None:
        from ..runtime.migration import MigrationContext
        from sqlalchemy.engine.default import DefaultDialect

        migration_context = MigrationContext.configure(
            dialect=DefaultDialect()
        )

    autogen_context = AutogenContext(migration_context, opts=opts)
    autogen_context.imports = set(imports)
    return render._indent(
        render._render_cmd_body(up_or_down_op, autogen_context)
    )


def _render_migration_diffs(
    context: MigrationContext, template_args: dict[Any, Any]
) -> None:
    """legacy, used by test_autogen_composition at the moment"""

    autogen_context = AutogenContext(context)

    upgrade_ops = ops.UpgradeOps([])
    compare._produce_net_changes(autogen_context, upgrade_ops)

    migration_script = ops.MigrationScript(
        rev_id=None,
        upgrade_ops=upgrade_ops,
        downgrade_ops=upgrade_ops.reverse(),
    )

    render._render_python_into_templatevars(
        autogen_context, migration_script, template_args
    )


class AutogenContext:
    """Maintains configuration and state that's specific to an
    autogenerate operation."""

    metadata: MetaData | Sequence[MetaData] | None = None
    """The :class:`~sqlalchemy.schema.MetaData` object
    representing the destination.

    This object is the one that is passed within ``env.py``
    to the :paramref:`.EnvironmentContext.configure.target_metadata`
    parameter.  It represents the structure of :class:`.Table` and other
    objects as stated in the current database model, and represents the
    destination structure for the database being examined.

    While the :class:`~sqlalchemy.schema.MetaData` object is primarily
    known as a collection of :class:`~sqlalchemy.schema.Table` objects,
    it also has an :attr:`~sqlalchemy.schema.MetaData.info` dictionary
    that may be used by end-user schemes to store additional schema-level
    objects that are to be compared in custom autogeneration schemes.

    """

    connection: Connection | None = None
    """The :class:`~sqlalchemy.engine.base.Connection` object currently
    connected to the database backend being compared.

    This is obtained from the :attr:`.MigrationContext.bind` and is
    ultimately set up in the ``env.py`` script.

    """

    dialect: Dialect
    """The :class:`~sqlalchemy.engine.Dialect` object currently in use.

    This is normally obtained from the
    :attr:`~sqlalchemy.engine.base.Connection.dialect` attribute.

    """

    imports: set[str] = None  # type: ignore[assignment]
    """A ``set()`` which contains string Python import directives.

    The directives are to be rendered into the ``${imports}`` section
    of a script template.  The set is normally empty and can be modified
    within hooks such as the
    :paramref:`.EnvironmentContext.configure.render_item` hook.

    .. seealso::

        :ref:`autogen_render_types`

    """

    migration_context: MigrationContext
    """The :class:`.MigrationContext` established by the ``env.py`` script."""

    comparators: PriorityDispatcher

    column_renames: ColumnRenameMap
    """The explicit column rename mapping in use for this autogenerate
    operation, derived from the column_renames option."""

    def __init__(
        self,
        migration_context: MigrationContext,
        metadata: MetaData | Sequence[MetaData] | None = None,
        opts: dict[str, Any] | None = None,
        autogenerate: bool = True,
    ) -> None:
        if (
            autogenerate
            and migration_context is not None
            and migration_context.as_sql
        ):
            raise util.CommandError(
                "autogenerate can't use as_sql=True as it prevents querying "
                "the database for schema information"
            )

        # branch off from the "global" comparators.  This collection
        # is empty in Alembic except that it is populated by third party
        # extensions that don't use the plugin system.  so we will build
        # off of whatever is in there.
        if autogenerate:
            self.comparators = compare.comparators.branch()
            Plugin.populate_autogenerate_priority_dispatch(
                self.comparators,
                include_plugins=migration_context.opts.get(
                    "autogenerate_plugins", ["alembic.autogenerate.*"]
                ),
            )

        if opts is None:
            opts = migration_context.opts

        self.metadata = metadata = (
            opts.get("target_metadata", None) if metadata is None else metadata
        )

        if (
            autogenerate
            and metadata is None
            and migration_context is not None
            and migration_context.script is not None
        ):
            raise util.CommandError(
                "Can't proceed with --autogenerate option; environment "
                "script %s does not provide "
                "a MetaData object or sequence of objects to the context."
                % (migration_context.script.env_py_location)
            )

        include_object = opts.get("include_object", None)
        include_name = opts.get("include_name", None)

        object_filters = []
        name_filters = []
        if include_object:
            object_filters.append(include_object)
        if include_name:
            name_filters.append(include_name)

        self._object_filters = object_filters
        self._name_filters = name_filters

        self.migration_context = migration_context
        self.connection = self.migration_context.bind
        self.dialect = self.migration_context.dialect

        self.imports = set()
        self.opts: dict[str, Any] = opts
        self._has_batch: bool = False
        self.column_renames = ColumnRenameMap.from_user_input(
            opts.get("column_renames")
        )
        self._active_renames_cache: dict[
            tuple[str | None, str], dict[str, str]
        ] = {}

    @util.memoized_property
    def inspector(self) -> Inspector:
        if self.connection is None:
            raise TypeError(
                "can't return inspector as this "
                "AutogenContext has no database connection"
            )
        return inspect(self.connection)

    def _renames_for_table(
        self, schema: str | None, tname: str
    ) -> dict[str, str]:
        """Return the validated, execution-ordered old->new column rename
        mapping in effect for the given table, or an empty dict if there
        are no renames.

        """
        if not self.column_renames:
            return {}
        default_schema = self.inspector.bind.dialect.default_schema_name
        return self.column_renames.renames_for_table(
            schema, tname, default_schema
        )

    def active_renames_for_table(
        self, schema: str | None, tname: str
    ) -> dict[str, str]:
        """Return the column renames for the given table that are still
        active, meaning they have not yet been applied to the database
        and are consistent with the target metadata.

        This is the mapping that all comparators (columns, indexes,
        constraints, foreign keys) use so that renamed columns are
        treated consistently; renames that are already applied or that
        do not apply to the current metadata are excluded.

        """
        renames = self._renames_for_table(schema, tname)
        if not renames:
            return {}

        cache = self._active_renames_cache
        cache_key = (schema, tname)
        if cache_key in cache:
            return cache[cache_key]

        try:
            conn_column_names = {
                col["name"]
                for col in self.inspector.get_columns(tname, schema=schema)
            }
        except NoSuchTableError:
            conn_column_names = None

        metadata_table = self._metadata_table_for(schema, tname)
        if conn_column_names is None or metadata_table is None:
            raise util.CommandError(
                "Column rename mapping refers to table %r which is not "
                "present in both the database and the target metadata; "
                "rename source not found" % (tname,)
            )

        metadata_column_names = {
            c.name for c in metadata_table.c if not c.system
        }
        active = renames_mod.validate_table_renames(
            renames, schema, tname, conn_column_names, metadata_column_names
        )
        cache[cache_key] = active
        return active

    def _metadata_table_for(
        self, schema: str | None, tname: str
    ) -> Table | None:
        default_schema = self.inspector.bind.dialect.default_schema_name
        for key in (
            sqla_compat._get_table_key(tname, schema),
            sqla_compat._get_table_key(tname, default_schema),
        ):
            if key in self.table_key_to_table:
                return self.table_key_to_table[key]
        return None

    def referred_table_renames(
        self, schema: str | None, tname: str
    ) -> dict[str, str]:
        """Return the old->new column renames to apply to reflected
        foreign key references that point at the given table.

        A reflected foreign key reference is translated whenever the
        target metadata carries the new column name and no longer
        carries the old one.  Unlike active_renames_for_table this does
        not consider the current database state of the referred table,
        so that references that are stale in the database (e.g. after a
        table-recreating rename on SQLite) still compare correctly
        against the metadata.

        """
        renames = self._renames_for_table(schema, tname)
        if not renames:
            return {}
        metadata_table = self._metadata_table_for(schema, tname)
        if metadata_table is None:
            return {}
        metadata_column_names = {
            c.name for c in metadata_table.c if not c.system
        }
        return {
            old_name: new_name
            for old_name, new_name in renames.items()
            if new_name in metadata_column_names
            and old_name not in metadata_column_names
        }

    @contextlib.contextmanager
    def _within_batch(self) -> Iterator[None]:
        self._has_batch = True
        yield
        self._has_batch = False

    def run_name_filters(
        self,
        name: str | None,
        type_: NameFilterType,
        parent_names: NameFilterParentNames,
    ) -> bool:
        """Run the context's name filters and return True if the targets
        should be part of the autogenerate operation.

        This method should be run for every kind of name encountered within the
        reflection side of an autogenerate operation, giving the environment
        the chance to filter what names should be reflected as database
        objects.  The filters here are produced directly via the
        :paramref:`.EnvironmentContext.configure.include_name` parameter.

        """
        if "schema_name" in parent_names:
            if type_ == "table":
                table_name = name
            else:
                table_name = parent_names.get("table_name", None)
            if table_name:
                schema_name = parent_names["schema_name"]
                if schema_name:
                    parent_names["schema_qualified_table_name"] = "%s.%s" % (
                        schema_name,
                        table_name,
                    )
                else:
                    parent_names["schema_qualified_table_name"] = table_name

        for fn in self._name_filters:
            if not fn(name, type_, parent_names):
                return False
        else:
            return True

    def run_object_filters(
        self,
        object_: SchemaItem,
        name: sqla_compat._ConstraintName,
        type_: NameFilterType,
        reflected: bool,
        compare_to: SchemaItem | None,
    ) -> bool:
        """Run the context's object filters and return True if the targets
        should be part of the autogenerate operation.

        This method should be run for every kind of object encountered within
        an autogenerate operation, giving the environment the chance
        to filter what objects should be included in the comparison.
        The filters here are produced directly via the
        :paramref:`.EnvironmentContext.configure.include_object` parameter.

        """
        for fn in self._object_filters:
            if not fn(object_, name, type_, reflected, compare_to):
                return False
        else:
            return True

    run_filters = run_object_filters

    @util.memoized_property
    def sorted_tables(self) -> list[Table]:
        """Return an aggregate of the :attr:`.MetaData.sorted_tables`
        collection(s).

        For a sequence of :class:`.MetaData` objects, this
        concatenates the :attr:`.MetaData.sorted_tables` collection
        for each individual :class:`.MetaData`  in the order of the
        sequence.  It does **not** collate the sorted tables collections.

        """
        result = []
        for m in util.to_list(self.metadata):
            result.extend(m.sorted_tables)
        return result

    @util.memoized_property
    def table_key_to_table(self) -> dict[str, Table]:
        """Return an aggregate  of the :attr:`.MetaData.tables` dictionaries.

        The :attr:`.MetaData.tables` collection is a dictionary of table key
        to :class:`.Table`; this method aggregates the dictionary across
        multiple :class:`.MetaData` objects into one dictionary.

        Duplicate table keys are **not** supported; if two :class:`.MetaData`
        objects contain the same table key, an exception is raised.

        """
        result: dict[str, Table] = {}
        for m in util.to_list(self.metadata):
            intersect = set(result).intersection(set(m.tables))
            if intersect:
                raise ValueError(
                    "Duplicate table keys across multiple "
                    "MetaData objects: %s"
                    % (", ".join('"%s"' % key for key in sorted(intersect)))
                )

            result.update(m.tables)
        return result


class RevisionContext:
    """Maintains configuration and state that's specific to a revision
    file generation operation."""

    generated_revisions: list[MigrationScript]
    process_revision_directives: ProcessRevisionDirectiveFn | None

    def __init__(
        self,
        config: Config,
        script_directory: ScriptDirectory,
        command_args: dict[str, Any],
        process_revision_directives: None | (
            ProcessRevisionDirectiveFn
        ) = None,
    ) -> None:
        self.config = config
        self.script_directory = script_directory
        self.command_args = command_args
        self.process_revision_directives = process_revision_directives
        self.template_args = {
            "config": config  # Let templates use config for
            # e.g. multiple databases
        }
        self.generated_revisions = [self._default_revision()]

    def _to_script(self, migration_script: MigrationScript) -> Script | None:
        template_args: dict[str, Any] = self.template_args.copy()

        if getattr(migration_script, "_needs_render", False):
            autogen_context = self._last_autogen_context

            # clear out existing imports if we are doing multiple
            # renders
            autogen_context.imports = set()
            if migration_script.imports:
                autogen_context.imports.update(migration_script.imports)
            render._render_python_into_templatevars(
                autogen_context, migration_script, template_args
            )

        assert migration_script.rev_id is not None
        return self.script_directory.generate_revision(
            migration_script.rev_id,
            migration_script.message,
            refresh=True,
            head=migration_script.head,
            splice=migration_script.splice,
            branch_labels=migration_script.branch_label,
            version_path=migration_script.version_path,
            depends_on=migration_script.depends_on,
            **template_args,
        )

    def run_autogenerate(
        self, rev: _GetRevArg, migration_context: MigrationContext
    ) -> None:
        self._run_environment(rev, migration_context, True)

    def run_no_autogenerate(
        self, rev: _GetRevArg, migration_context: MigrationContext
    ) -> None:
        self._run_environment(rev, migration_context, False)

    def _run_environment(
        self,
        rev: _GetRevArg,
        migration_context: MigrationContext,
        autogenerate: bool,
    ) -> None:
        if autogenerate:
            if self.command_args["sql"]:
                raise util.CommandError(
                    "Using --sql with --autogenerate does not make any sense"
                )
            if set(self.script_directory.get_revisions(rev)) != set(
                self.script_directory.get_revisions("heads")
            ):
                raise util.CommandError("Target database is not up to date.")

        upgrade_token = migration_context.opts["upgrade_token"]
        downgrade_token = migration_context.opts["downgrade_token"]

        migration_script = self.generated_revisions[-1]
        if not getattr(migration_script, "_needs_render", False):
            migration_script.upgrade_ops_list[-1].upgrade_token = upgrade_token
            migration_script.downgrade_ops_list[-1].downgrade_token = (
                downgrade_token
            )
            migration_script._needs_render = True
        else:
            migration_script._upgrade_ops.append(
                ops.UpgradeOps([], upgrade_token=upgrade_token)
            )
            migration_script._downgrade_ops.append(
                ops.DowngradeOps([], downgrade_token=downgrade_token)
            )

        autogen_context = AutogenContext(
            migration_context, autogenerate=autogenerate
        )
        self._last_autogen_context: AutogenContext = autogen_context

        if autogenerate:
            compare._populate_migration_script(
                autogen_context, migration_script
            )

        if self.process_revision_directives:
            self.process_revision_directives(
                migration_context, rev, self.generated_revisions
            )

        hook = migration_context.opts["process_revision_directives"]
        if hook:
            hook(migration_context, rev, self.generated_revisions)

        for migration_script in self.generated_revisions:
            migration_script._needs_render = True

    def _default_revision(self) -> MigrationScript:
        command_args: dict[str, Any] = self.command_args
        op = ops.MigrationScript(
            rev_id=command_args["rev_id"] or util.rev_id(),
            message=command_args["message"],
            upgrade_ops=ops.UpgradeOps([]),
            downgrade_ops=ops.DowngradeOps([]),
            head=command_args["head"],
            splice=command_args["splice"],
            branch_label=command_args["branch_label"],
            version_path=command_args["version_path"],
            depends_on=command_args["depends_on"],
        )
        return op

    def generate_scripts(self) -> Iterator[Script | None]:
        for generated_revision in self.generated_revisions:
            yield self._to_script(generated_revision)

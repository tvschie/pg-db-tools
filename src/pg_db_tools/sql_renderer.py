from itertools import chain

from pg_db_tools import iter_join
from pg_db_tools.graph import database_to_graph
from pg_db_tools.pg_types import PgEnumType, PgTable, PgFunction, PgView, \
    PgCompositeType, PgAggregate, PgSequence, PgSchema, PgRole, PgTrigger, \
    PgCast, PgSetting, PgRow

def render_setting_sql(pg_setting):
    return [
        "DO $$ BEGIN",
        "EXECUTE 'ALTER DATABASE ' || current_database() || ' SET {} TO {}';".format(pg_setting.name, pg_setting.value),
        "END; $$;\n",
        "SET {} TO {};".format(pg_setting.name, pg_setting.value)
        ]


def render_table_sql(table):
    options = []
    post_options = []

    if table.inherits:
        post_options.append('INHERITS ({}.{})'.format(
            quote_ident(table.inherits.schema.name),
            quote_ident(table.inherits.name)
        ))

    yield (
        'CREATE TABLE {options}{ident}\n'
        '(\n'
        '{columns_part}\n'
        '){post_options};\n'
    ).format(
        options=''.join('{} '.format(option) for option in options),
        ident='{}.{}'.format(
            quote_ident(table.schema.name), quote_ident(table.name)
        ),
        columns_part=',\n'.join(table_defining_components(table)),
        post_options=' '.join(post_options)
    )

    if table.description:
        yield (
            'COMMENT ON TABLE {} IS {};\n'
        ).format(
            '{}.{}'.format(
                quote_ident(table.schema.name), quote_ident(table.name)
            ),
            quote_string(escape_string(table.description))
        )

    if table.indexes:
        for index in table.indexes:
            yield ('{};\n'.format(index.definition))

    if table.owner:
        yield ('ALTER TABLE {}.{} OWNER TO {};\n'.format(
            quote_ident(table.schema.name),
            quote_ident(table.name),
            table.owner.name
        ))

    for privilege in table.privs:
        yield('GRANT {} ON TABLE {}.{} TO {};\n'.format(
            privilege[1],
            quote_ident(table.schema.name),
            quote_ident(table.name),
            privilege[0]
        ))


def table_defining_components(table):
    for column_data in table.columns:
        yield '  {}'.format(render_column_definition(column_data))

    if table.primary_key:
        yield '  PRIMARY KEY ({})'.format(', '.join(table.primary_key.columns))

    if table.unique:
        for unique_constraint in table.unique:
            yield '  UNIQUE ({})'.format(
                ', '.join(unique_constraint['columns'])
            )

    if table.check:
        for check_constraint in table.check:
            yield '  CHECK ({})'.format(check_constraint['expression'])

    if table.exclude:
        for exclude_constraint in table.exclude:
            yield '  {}'.format(
                render_exclude_constraint(exclude_constraint)
            )


def render_column_definition(column):
    parts = [
        quote_ident(column.name),
        str(column.data_type)
    ]

    if column.nullable is False:
        parts.append('NOT NULL')

    if column.default:
        parts.append('DEFAULT {}'.format(column.default))

    return ' '.join(parts)


def render_composite_type_column_definition(column):
    return '{} {}'.format(quote_ident(column.name), column.data_type)


def render_exclude_constraint(exclude_data):
    parts = ['EXCLUDE ']

    if exclude_data.get('index_method'):
        parts.append('USING {index_method} '.format(**exclude_data))

    parts.append(
        '({})'.format(
            ', '.join(
                '{exclude_element} WITH {operator}'.format(**e)
                for e in exclude_data['exclusions']
            )
        )
    )

    return ''.join(parts)


def render_function_sql(pg_function):
    returns_part = '    RETURNS '

    table_arguments = [
        argument for argument in pg_function.arguments if argument.mode == 't'
    ]

    if table_arguments:
        returns_part += 'TABLE({})'.format(', '.join(render_argument(argument) for argument in table_arguments))
    else:
        if pg_function.returns_set:
            returns_part += 'SETOF '

        returns_part += str(pg_function.return_type)

    return [
        'CREATE FUNCTION "{}"."{}"({})'.format(
            pg_function.schema.name, pg_function.name,
            ', '.join(render_argument(argument) for argument in pg_function.arguments if argument.mode in ('i', 'o', 'b', 'v'))
        ),
        returns_part,
        'AS $function$' if '$$' in str(pg_function.src) else 'AS $$',
        str(pg_function.src),
        '${}$ LANGUAGE {} {}{};'.format(
            'function' if '$$' in str(pg_function.src) else '',
            pg_function.language,
            pg_function.volatility.upper(),
            ' STRICT' if pg_function.strict else ''
        )
    ]


def render_trigger_sql(pg_trigger):
    when = "INSTEAD OF" if pg_trigger.when == 'instead' else pg_trigger.when.upper()
    return [
        'CREATE TRIGGER {}'.format(pg_trigger.name),
        '{} {} ON {}'.format(when, " OR ".join(pg_trigger.events).upper(), pg_trigger.table),
        'FOR EACH {}'.format(pg_trigger.affecteach.upper()),
        'EXECUTE PROCEDURE {}();'.format(pg_trigger.function)
    ]


def render_sequence_sql(pg_sequence):
    return [
        'CREATE SEQUENCE {}.{}'.format(pg_sequence.schema.name, pg_sequence.name),
        'START WITH {}'.format(pg_sequence.start_value),
        'INCREMENT BY {}'.format(pg_sequence.increment),
        'NO MINVALUE' if pg_sequence.minimum_value is None else 'MINVALUE {}'.format(pg_sequence.minimum_value),
        'NO MAXVALUE' if pg_sequence.maximum_value is None else 'MAXVALUE {}'.format(pg_sequence.maximum_value),
        'CACHE 1;'
    ]    


def render_cast_sql(pg_cast):
    return [
        'CREATE CAST ({} AS {}) WITH FUNCTION {}({}){};'.format(
            pg_cast.source,
            pg_cast.target,
            pg_cast.function,
            pg_cast.source,
            ' AS IMPLICIT' if pg_cast.implicit else ''
        )]


def render_row_sql(pg_row):
    return [
        'INSERT INTO {} ({}) VALUES ({});'.format(
            pg_row.table,
            ", ".join(pg_row.values.keys()),
            ", ".join(pg_row.values.values())
        )]


def render_role_sql(pg_role):
    attributes = (["LOGIN"] if pg_role.login else []) +\
                 [
                     "SUPERUSER" if pg_role.super else "NOSUPERUSER",
                     "INHERIT" if pg_role.inherit else "NOINHERIT",
                     "CREATEDB" if pg_role.createdb else "NOCREATEDB",
                     "CREATEROLE;" if pg_role.createrole else "NOCREATEROLE;"
                 ]
    return [
        "DO\n$$\nBEGIN",
        "IF NOT EXISTS(SELECT * FROM pg_roles WHERE rolname = '{}') THEN".format(pg_role.name),
        "CREATE ROLE {}".format(pg_role.name),
        " ".join(attribute for attribute in attributes),
        "END IF;\nEND\n$$;",
        ] +\
        [ "\nGRANT {} TO {};".format(membership.name, pg_role.name) for membership in pg_role.membership ]


def render_view_sql(pg_view):
    return [
        'CREATE VIEW "{}"."{}" AS'.format(pg_view.schema.name, pg_view.name),
        pg_view.view_query
    ]


def render_composite_type_sql(pg_composite_type):
    yield (
        'CREATE TYPE {ident} AS (\n'
        '{columns_part}\n'
        ');\n'
    ).format(
        ident='{}.{}'.format(quote_ident(pg_composite_type.schema.name), quote_ident(pg_composite_type.name)),
        columns_part=',\n'.join(
            '  {}'.format(render_composite_type_column_definition(column_data))
            for column_data in pg_composite_type.columns
        )
    )


def render_enum_type_sql(pg_enum_type):
    yield (
        'CREATE TYPE {ident} AS ENUM (\n'
        '{labels_part}\n'
        ');\n'
    ).format(
        ident='{}.{}'.format(quote_ident(pg_enum_type.schema.name), quote_ident(pg_enum_type.name)),
        labels_part=',\n'.join('  {}'.format(quote_string(label)) for label in pg_enum_type.labels)
    )


def render_aggregate_sql(pg_aggregate):
    properties = [
        '    SFUNC = {}'.format(pg_aggregate.sfunc.ident()),
        '    STYPE = {}'.format(pg_aggregate.stype.ident())
    ]

    yield (
        'CREATE AGGREGATE {ident} ({arguments}) (\n'
        '{properties}\n'
        ');\n'
    ).format(
        ident=pg_aggregate.ident(),
        arguments=', '.join(render_argument(argument) for argument in pg_aggregate.arguments),
        properties=',\n'.join(properties)
    )


def render_argument(pg_argument):
    if pg_argument.name is None:
        return str(pg_argument.data_type.ident())
    else:
        return '{} {}{}'.format(
            quote_ident(pg_argument.name),
            str(pg_argument.data_type.ident()),
            '' if pg_argument.default is None else ' DEFAULT {}'.format(pg_argument.default)
        )


def render_schema_sql(pg_schema):
    yield (
        'CREATE SCHEMA IF NOT EXISTS {ident};'
    ).format(
        ident = quote_ident(pg_schema.name)
    )


sql_renderers = {
    PgSetting: render_setting_sql,
    PgSchema: render_schema_sql,
    PgTable: render_table_sql,
    PgFunction: render_function_sql,
    PgSequence: render_sequence_sql,
    PgView: render_view_sql,
    PgCompositeType: render_composite_type_sql,
    PgEnumType: render_enum_type_sql,
    PgAggregate: render_aggregate_sql,
    PgTrigger: render_trigger_sql,
    PgRole: render_role_sql,
    PgCast: render_cast_sql,
    PgRow: render_row_sql,
}


class SqlRenderer:
    def __init__(self):
        self.if_not_exists = False

    def render(self, out_file, database):
        graph = database_to_graph(database)

        rendered_chunks = self.render_chunks(database)

        out_file.writelines(rendered_chunks)

    def render_chunks(self, database):
        return iter_join(
            '\n',
            chain(*self.render_chunk_sets(database))
        )

    def render_chunk_sets(self, database):
        yield self.create_extension_statements(database)

        for pg_object in database.objects:
            yield '\n'
            yield sql_renderers[type(pg_object)](pg_object)

        for schema in sorted(database.schemas.values(), key=lambda s: s.name):
            for table in schema.tables:
                for index, foreign_key in enumerate(table.foreign_keys):
                    yield SqlRenderer.render_foreign_key(
                        index, schema, table, foreign_key
                    )

    @staticmethod
    def render_foreign_key(index, schema, table, foreign_key):
        try:
            key_name=foreign_key.name
        except AttributeError:
            key_name='{}_{}_fk_{}'.format(schema.name, table.name, index)
        return [(
            'ALTER TABLE {schema_name}.{table_name} '
            'ADD CONSTRAINT {key_name} '
            'FOREIGN KEY ({columns}) '
            'REFERENCES {ref_schema_name}.{ref_table_name} ({ref_columns}){on_update}{on_delete};'
        ).format(
            schema_name=quote_ident(schema.name),
            table_name=quote_ident(table.name),
            key_name=quote_ident(key_name),
            columns=', '.join(foreign_key.columns),
            ref_schema_name=quote_ident(
                foreign_key.get_name(foreign_key.schema)
            ),
            ref_table_name=quote_ident(
                foreign_key.get_name(foreign_key.ref_table)
            ),
            ref_columns=', '.join(foreign_key.ref_columns),
            on_update = ' ON UPDATE {}'.format(foreign_key.on_update.upper()) if foreign_key.on_update else '',
            on_delete = ' ON DELETE {}'.format(foreign_key.on_delete.upper()) if foreign_key.on_delete else '',
        )]

    def create_extension_statements(self, database):
        options = []

        if self.if_not_exists:
            options.append('IF NOT EXISTS')

        for extension_name in database.extensions:
            yield 'CREATE EXTENSION {options}{extension_name};\n'.format(
                options=''.join('{} '.format(option) for option in options),
                extension_name=extension_name
            )


def quote_ident(ident):
    return '"' + ident + '"'


def quote_string(string):
    return "'" + string + "'"


def escape_string(string):
    return string.replace("'", "''")

# import datetime
import collections
import itertools

# from itertools import dropwhile
# import json
import logging
import copy

# import uuid

import singer
import singer.metrics as metrics
import singer.schema

# from singer import bookmarks
from singer import metadata
from singer import utils
from singer.schema import Schema
from singer.catalog import Catalog, CatalogEntry

import tap_db2.sync_strategies.common as common
import tap_db2.sync_strategies.full_table as full_table
import tap_db2.sync_strategies.incremental as incremental
import tap_db2.sync_strategies.logical as logical

from tap_db2.connection import (
    # connect_with_backoff,
    get_azure_sql_engine,
)


Column = collections.namedtuple(
    "Column",
    [
        "table_schema",
        "table_name",
        "column_name",
        "data_type",
        "character_maximum_length",
        "numeric_scale",
        "is_primary_key",
    ],
)

REQUIRED_CONFIG_KEYS = [
    "username",
    "password",
    "hostname",
    "port",
    "database",
]

LOGGER = singer.get_logger()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Define data types

# Full list
#BIGINT - i
#BLOB - ignore for now
#CHARACTER - s
#CLOB - ignore for now
#DATE - d
#DECIMAL - f
#DOUBLE - f
#INTEGER - i
#SMALLINT - i
#TIMESTAMP - d
#VARCHAR - s
#XML - s

STRING_TYPES = set(
    [
        "character",
        "varchar",
        "xml",
    ]
)

BYTES_FOR_INTEGER_TYPE = {
    "smallint": 2,
    "integer": 4,
    "bigint": 8,
}

FLOAT_TYPES = set(
    [
        "decimal",
        "double",
    ]
)

DATETIME_TYPES = set(
    [
        "timestamp",
    ]
)

DATE_TYPES = set(
        [
            "date",
        ]
)

def default_date_format():
    return False

def schema_for_column(c,config):
    """Returns the Schema object for the given Column."""
    data_type = c.data_type.strip().lower()

    inclusion = "available"

    use_date_data_type_format = config.get('use_date_datatype') or default_date_format()

    if c.is_primary_key == 1:
        inclusion = "automatic"

    result = Schema(inclusion=inclusion)

    if data_type in BYTES_FOR_INTEGER_TYPE:
        result.type = ["null", "integer"]
        bits = BYTES_FOR_INTEGER_TYPE[data_type] * 8
        result.minimum = 0 - 2 ** (bits - 1)
        result.maximum = 2 ** (bits - 1) - 1

    elif data_type in FLOAT_TYPES:
        result.type = ["null", "number"]
        result.multipleOf = 10 ** (0 - (c.numeric_scale or 6))

    elif data_type in STRING_TYPES:
        result.type = ["null", "string"]
        result.maxLength = c.character_maximum_length

    elif data_type in DATETIME_TYPES:
        result.type = ["null", "string"]
        result.format = "date-time"

    elif data_type in DATE_TYPES:
        result.type = ["null","string"]
        if use_date_data_type_format:
            result.format = "date"
        else:
            result.format = "date-time"

    else:
        result = Schema(
            None,
            inclusion="unsupported",
            description="Unsupported column type",
        )
    return result


def create_column_metadata(cols, config):
    mdata = {}
    mdata = metadata.write(mdata, (), "selected-by-default", False)
    for c in cols:
        schema = schema_for_column(c, config)
        mdata = metadata.write(
            mdata,
            ("properties", c.column_name),
            "selected-by-default",
            schema.inclusion != "unsupported",
        )
        mdata = metadata.write(
            mdata,
            ("properties", c.column_name),
            "sql-datatype",
            c.data_type.strip().lower(),
        )

    return metadata.to_list(mdata)


def discover_catalog(mssql_conn, config):
    """Returns a Catalog describing the structure of the database."""
    LOGGER.info("Preparing Catalog")

    with mssql_conn.connect() as open_conn:
        LOGGER.info("Fetching tables")
        tables_results = open_conn.execute(
            """
            SELECT
                RTRIM(TABSCHEMA) AS TABLE_SCHEMA,
                TABNAME AS TABLE_NAME,
                TYPE AS TABLE_TYPE
            FROM SYSCAT.TABLES t
            WHERE t.TABSCHEMA NOT IN (
                'SYSTOOLS',
                'SYSIBM',
                'SYSCAT',
                'SYSPUBLIC',
                'SYSSTAT',
                'SYSIBMADM'
            )
            """
        )
        table_info = {}

        for (db, table, table_type) in tables_results.fetchall():
            if db not in table_info:
                table_info[db] = {}

            table_info[db][table] = {
                "row_count": None,
                "is_view": table_type == "V",
            }

            LOGGER.debug(table_info)
        LOGGER.info("Tables fetched, fetching columns")
        column_results = open_conn.execute(
            """
            SELECT
                RTRIM(t.TABSCHEMA) AS TABLE_SCHEMA,
                t.TABNAME AS TABLE_NAME,
                c.COLNAME AS COLUMN_NAME,
                c.TYPENAME AS DATA_TYPE,
                c.LENGTH AS CHARACTER_MAXIMUM_LENGTH,
                c."SCALE" AS NUMERIC_SCALE,
                CASE
                    WHEN c.KEYSEQ IS NOT NULL THEN 1
                    ELSE 0
                END AS IS_PRIMARY_KEY
            FROM 
            SYSCAT.TABLES t
            LEFT JOIN 
            SYSCAT.COLUMNS c
            ON c.TABNAME = t.TABNAME 
            AND c.TABSCHEMA = t.TABSCHEMA 
            WHERE t.TABSCHEMA NOT LIKE 'SYS%';
            """
        )
        columns = []
        rec = column_results.fetchone()
        LOGGER.info(rec)
        while rec is not None:
            columns.append(Column(*rec))
            rec = column_results.fetchone()
        LOGGER.info("Columns Fetched")
        entries = []
        for (k, cols) in itertools.groupby(
            columns, lambda c: (c.table_schema, c.table_name)
        ):
            cols = list(cols)
            (table_schema, table_name) = k
            schema = Schema(
                type="object",
                properties={c.column_name: schema_for_column(c, config) for c in cols},
            )
            md = create_column_metadata(cols, config)
            md_map = metadata.to_map(md)

            md_map = metadata.write(md_map, (), "database-name", table_schema)

            is_view = table_info[table_schema][table_name]["is_view"]

            if (
                table_schema in table_info
                and table_name in table_info[table_schema]
            ):
                row_count = table_info[table_schema][table_name].get(
                    "row_count"
                )

                if row_count is not None:
                    md_map = metadata.write(md_map, (), "row-count", row_count)

                md_map = metadata.write(md_map, (), "is-view", is_view)

            key_properties = [
                c.column_name for c in cols if c.is_primary_key == 1
            ]

            md_map = metadata.write(
                md_map, (), "table-key-properties", key_properties
            )

            entry = CatalogEntry(
                table=table_name,
                stream=table_name,
                metadata=metadata.to_list(md_map),
                tap_stream_id=common.generate_tap_stream_id(
                    table_schema, table_name
                ),
                schema=schema,
            )

            entries.append(entry)
    LOGGER.info("Catalog ready")
    return Catalog(entries)


def do_discover(mssql_conn, config):
    discover_catalog(mssql_conn, config).dump()


# TODO: Maybe put in a singer-db-utils library.
def desired_columns(selected, table_schema):

    """Return the set of column names we need to include in the SELECT.

    selected - set of column names marked as selected in the input catalog
    table_schema - the most recently discovered Schema for the table
    """
    all_columns = set()
    available = set()
    automatic = set()
    unsupported = set()

    for column, column_schema in table_schema.properties.items():
        all_columns.add(column)
        inclusion = column_schema.inclusion
        if inclusion == "automatic":
            automatic.add(column)
        elif inclusion == "available":
            available.add(column)
        elif inclusion == "unsupported":
            unsupported.add(column)
        else:
            raise Exception("Unknown inclusion " + inclusion)

    selected_but_unsupported = selected.intersection(unsupported)
    if selected_but_unsupported:
        LOGGER.warning(
            "Columns %s were selected but are not supported. Skipping them.",
            selected_but_unsupported,
        )

    selected_but_nonexistent = selected.difference(all_columns)
    if selected_but_nonexistent:
        LOGGER.warning(
            "Columns %s were selected but do not exist.",
            selected_but_nonexistent,
        )

    not_selected_but_automatic = automatic.difference(selected)
    if not_selected_but_automatic:
        LOGGER.warning(
            "Columns %s are primary keys but were not selected. Adding them.",
            not_selected_but_automatic,
        )

    return selected.intersection(available).union(automatic)


def is_valid_currently_syncing_stream(selected_stream, state):
    return True


def resolve_catalog(discovered_catalog, streams_to_sync):
    result = Catalog(streams=[])

    # Iterate over the streams in the input catalog and match each one up
    # with the same stream in the discovered catalog.
    for catalog_entry in streams_to_sync:
        catalog_metadata = metadata.to_map(catalog_entry.metadata)
        replication_key = catalog_metadata.get((), {}).get("replication-key")

        discovered_table = discovered_catalog.get_stream(
            catalog_entry.tap_stream_id
        )
        database_name = common.get_database_name(catalog_entry)

        if not discovered_table:
            LOGGER.warning(
                "Database %s table %s was selected but does not exist",
                database_name,
                catalog_entry.table,
            )
            continue

        selected = {
            k
            for k, v in discovered_table.schema.properties.items()
            if common.property_is_selected(catalog_entry, k)
            or k == replication_key
        }

        # These are the columns we need to select
        columns = desired_columns(selected, discovered_table.schema)
        result.streams.append(
            CatalogEntry(
                tap_stream_id=catalog_entry.tap_stream_id,
                metadata=catalog_entry.metadata,
                stream=catalog_entry.tap_stream_id,
                table=catalog_entry.table,
                schema=Schema(
                    type="object",
                    properties={
                        col: discovered_table.schema.properties[col]
                        for col in columns
                    },
                ),
            )
        )

    return result


def get_non_binlog_streams(mssql_conn, catalog, config, state):
    """Returns the Catalog of data we're going to sync for all SELECT-based
    streams (i.e. INCREMENTAL, FULL_TABLE, and LOG_BASED that require a
    historical sync). LOG_BASED streams that require a historical sync are
    inferred from lack of any state.

    Using the Catalog provided from the input file, this function will return a
    Catalog representing exactly which tables and columns that will be emitted
    by SELECT-based syncs. This is achieved by comparing the input Catalog to a
    freshly discovered Catalog to determine the resulting Catalog.

    The resulting Catalog will include the following any streams marked as
    "selected" that currently exist in the database. Columns marked as
    "selected" and those labled "automatic" (e.g. primary keys and replication
    keys) will be included. Streams will be prioritized in the following order:
      1. currently_syncing if it is SELECT-based
      2. any streams that do not have state
      3. any streams that do not have a replication method of LOG_BASED

    """
    discovered = discover_catalog(mssql_conn, config)

    # Filter catalog to include only selected streams
    selected_streams = list(
        filter(lambda s: common.stream_is_selected(s), catalog.streams)
    )
    streams_with_state = []
    streams_without_state = []

    for stream in selected_streams:
        stream_metadata = metadata.to_map(stream.metadata)
        # if stream_metadata.table in ["aagaggpercols", "aagaggdef"]:
        for k, v in stream_metadata.get((), {}).items():
            LOGGER.info(f"{k}: {v}")
            # LOGGER.info(stream_metadata.get((), {}).get(
            #   "table-key-properties"
            # ))
        # replication_method = stream_metadata.get((), {}).get(
        #     "replication-method"
        # )
        stream_state = state.get("bookmarks", {}).get(stream.tap_stream_id)

        if not stream_state:
            streams_without_state.append(stream)
        else:
            streams_with_state.append(stream)

    # If the state says we were in the middle of processing a stream, skip
    # to that stream. Then process streams without prior state and finally
    # move onto streams with state (i.e. have been synced in the past)
    currently_syncing = singer.get_currently_syncing(state)

    # prioritize streams that have not been processed
    ordered_streams = streams_without_state + streams_with_state

    if currently_syncing:
        currently_syncing_stream = list(
            filter(
                lambda s: s.tap_stream_id == currently_syncing
                and is_valid_currently_syncing_stream(s, state),
                streams_with_state,
            )
        )

        non_currently_syncing_streams = list(
            filter(
                lambda s: s.tap_stream_id != currently_syncing, ordered_streams
            )
        )

        streams_to_sync = (
            currently_syncing_stream + non_currently_syncing_streams
        )
    else:
        # prioritize streams that have not been processed
        streams_to_sync = ordered_streams

    return resolve_catalog(discovered, streams_to_sync)


def get_binlog_streams(mssql_conn, catalog, config, state):
    discovered = discover_catalog(mssql_conn, config)

    # selected_streams = list(
    #     filter(lambda s: common.stream_is_selected(s), catalog.streams)
    # )
    binlog_streams = []

    # for stream in selected_streams:
    #     stream_metadata = metadata.to_map(stream.metadata)
    #     replication_method = stream_metadata.get((), {}).get(
    #         "replication-method"
    #     )
    #     stream_state = state.get("bookmarks", {}).get(stream.tap_stream_id)

    return resolve_catalog(discovered, binlog_streams)


def write_schema_message(config, catalog_entry, bookmark_properties=[]):
    key_properties = common.get_key_properties(catalog_entry)

    table_stream = common.set_schema_mapping(config, catalog_entry.stream)

    singer.write_message(
        singer.SchemaMessage(
            stream=table_stream,
            schema=catalog_entry.schema.to_dict(),
            key_properties=key_properties,
            bookmark_properties=bookmark_properties,
        )
    )


def do_sync_incremental(mssql_conn, config, catalog_entry, state, columns):
    md_map = metadata.to_map(catalog_entry.metadata)
    # stream_version = common.get_stream_version(
    #     catalog_entry.tap_stream_id, state
    # )

    replication_key = md_map.get((), {}).get("replication-key")
    write_schema_message(
        config,
        catalog_entry=catalog_entry,
        bookmark_properties=[replication_key],
    )
    LOGGER.info("Schema written")
    incremental.sync_table(mssql_conn, config, catalog_entry, state, columns)

    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))


def do_sync_full_table(mssql_conn, config, catalog_entry, state, columns):
    # key_properties = common.get_key_properties(catalog_entry)

    write_schema_message(config, catalog_entry)

    stream_version = common.get_stream_version(
        catalog_entry.tap_stream_id, state
    )

    full_table.sync_table(
        mssql_conn, config, catalog_entry, state, columns, stream_version
    )

    # Prefer initial_full_table_complete going forward
    singer.clear_bookmark(state, catalog_entry.tap_stream_id, "version")

    state = singer.write_bookmark(
        state, catalog_entry.tap_stream_id, "initial_full_table_complete", True
    )

    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))


def do_sync_log_based_table(mssql_conn, config, catalog_entry, state, columns):

    # key_properties = common.get_key_properties(catalog_entry)
    state = singer.set_currently_syncing(state, catalog_entry.tap_stream_id)
    write_schema_message(config, catalog_entry)

    # stream_version = common.get_stream_version(
    #     catalog_entry.tap_stream_id, state
    # )

    # initial instance of log_based connector class
    log_based = logical.log_based_sync(
        mssql_conn, config, catalog_entry, state, columns
    )

    # assert all of the log_based prereq's are met
    log_based.assert_log_based_is_enabled()

    # create state if none exists
    initial_full_table_complete = log_based.log_based_init_state()

    if not initial_full_table_complete:
        # set full_table_complete state to false and current_log_version
        # to current
        state = singer.write_bookmark(
            state,
            catalog_entry.tap_stream_id,
            "initial_full_table_complete",
            log_based.initial_full_table_complete,
        )
        state = singer.write_bookmark(
            state,
            catalog_entry.tap_stream_id,
            "current_log_version",
            log_based.current_log_version,
        )

        log_based.state = state

    initial_load = log_based.log_based_initial_full_table()

    if initial_load:
        do_sync_full_table(mssql_conn, config, catalog_entry, state, columns)
        state = singer.write_bookmark(
            state,
            catalog_entry.tap_stream_id,
            "initial_full_table_complete",
            True,
        )

        state = singer.write_bookmark(
            state,
            catalog_entry.tap_stream_id,
            "current_log_version",
            log_based.current_log_version,  # when the version is out of date,
            # this has gotten stuck instead of refreshing the table.
        )

    else:
        LOGGER.info("Continue log-based syncing")
        log_based.execute_log_based_sync()


def sync_non_binlog_streams(mssql_conn, non_binlog_catalog, config, state):

    for catalog_entry in non_binlog_catalog.streams:
        columns = list(catalog_entry.schema.properties.keys())

        if not columns:
            LOGGER.warning(
                "There are no columns selected for stream %s, skipping it.",
                catalog_entry.stream,
            )
            continue

        state = singer.set_currently_syncing(
            state, catalog_entry.tap_stream_id
        )

        # Emit a state message to indicate that we've started this stream
        singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

        md_map = metadata.to_map(catalog_entry.metadata)
        replication_method = md_map.get((), {}).get("replication-method")
        replication_key = md_map.get((), {}).get("replication-key")
        # primary_keys = md_map.get((), {}).get("table-key-properties")
        LOGGER.info(
            f"Table {catalog_entry.table} proposes {replication_method} sync"
        )
        if replication_method == "INCREMENTAL" and not replication_key:
            LOGGER.info(
                f"No replication key for {catalog_entry.table}, "
                "using full table replication"
            )
            replication_method = "FULL_TABLE"
        # Removing conditional check for primary keys - if a replication key
        # is already specified, we can allow incremental loads on views

        # if replication_method == "INCREMENTAL" and not primary_keys:
        #     LOGGER.info(
        #         f"No primary key for {catalog_entry.table}, "
        #           "using full table replication"
        #     )
        #     replication_method = "FULL_TABLE"
        LOGGER.info(
            f"Table {catalog_entry.table} will use {replication_method} sync"
        )

        database_name = common.get_database_name(catalog_entry)

        with metrics.job_timer("sync_table") as timer:
            timer.tags["database"] = database_name
            timer.tags["table"] = catalog_entry.table

            if replication_method == "INCREMENTAL":
                LOGGER.info(f"syncing {catalog_entry.table} incrementally")
                do_sync_incremental(
                    mssql_conn, config, catalog_entry, state, columns
                )
            elif replication_method == "FULL_TABLE":
                LOGGER.info(f"syncing {catalog_entry.table} full table")
                do_sync_full_table(
                    mssql_conn, config, catalog_entry, state, columns
                )
            elif replication_method == "LOG_BASED":
                LOGGER.info(
                    f"syncing {catalog_entry.table} using replication method "
                    "LOG_BASED"
                )
                do_sync_log_based_table(
                    mssql_conn, config, catalog_entry, state, columns
                )
            else:
                raise Exception(
                    "only INCREMENTAL and FULL TABLE replication methods are "
                    "supported"
                )

    state = singer.set_currently_syncing(state, None)
    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))


def do_sync(mssql_conn, config, catalog, state):
    LOGGER.info("Beginning sync")
    non_binlog_catalog = get_non_binlog_streams(
        mssql_conn, catalog, config, state
    )
    for entry in non_binlog_catalog.streams:
        LOGGER.info(f"Need to sync {entry.table}")
    sync_non_binlog_streams(mssql_conn, non_binlog_catalog, config, state)


def log_server_params(mssql_conn):
    with mssql_conn.connect() as open_conn:
        #
        # https://stackoverflow.com/questions/3821795/how-to-check-db2-version
        # two approaches possible - TABLE(sysproc.env_get_inst_info())
        #                      or - SYSIBMADM.ENV_INST_INFO
        server_parameters=[
                'INST_NAME',
                'IS_INST_PARTITIONABLE',
                'NUM_DBPARTITIONS',
                'INST_PTR_SIZE',
                'RELEASE_NUM',
                'SERVICE_LEVEL',
                'BLD_LEVEL',
                'PTF',
                'FIXPACK_NUM',
                'NUM_MEMBERS'
                ]
        try:
            row = open_conn.execute(
                """
                   SELECT {} FROM SYSIBMADM.ENV_INST_INFO
                """.format(','.join(server_parameters))
            )
            LOGGER.info(
                    "Server Parameters: " + ', '.join([p+': %s' for p in server_parameters]),
                *row.fetchone(),
            )
        except Exception as e:
            LOGGER.warning(
                "Encountered error checking server params. Error: (%s) %s",
                *e.args,
            )


def main_impl():
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)
    mssql_conn = get_azure_sql_engine(args.config)
    log_server_params(mssql_conn)

    if args.discover:
        do_discover(mssql_conn, args.config)
    elif args.catalog:
        state = args.state or {}
        do_sync(mssql_conn, args.config, args.catalog, state)
    elif args.properties:
        catalog = Catalog.from_dict(args.properties)
        state = args.state or {}
        do_sync(mssql_conn, args.config, catalog, state)
    else:
        LOGGER.info("No properties were selected")


def main():
    try:
        main_impl()
    except Exception as exc:
        LOGGER.critical(exc)
        raise exc

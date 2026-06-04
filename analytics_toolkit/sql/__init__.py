from .ddl.create_sql_table import (
    build_create_table_sql,
    build_create_table_sqls,
    create_sql_table,
)
from .ddl.extract_ddl import extract_ddl
from .orchestration.async_api import async_sql, parallel_sql
from .core.capabilities import (
    BACKEND_CAPABILITIES,
    format_support_matrix,
    support_matrix_rows,
)
from .execution.plans import (
    SqlOperationMetadata,
    SqlOperationResult,
    SqlPlan,
    SqlStatement,
    format_plan,
)
from .metadata.show_tables import show_tables
from .metadata.table_info import SqlTableInfo, table_info
from .core.types import BackendName, ConnectionKey, SqlTaskType, SqlText, TableName
from .dml.io.execute_read import execute_read
from .dml.io.execute_sql import execute_sql, execute_sql as execute
from .connection.config import (
    ConnectionValidationResult,
    airflow_connection_config,
    use_airflow_connections,
    validate_connections,
)
from .connection.errors import SqlOperationContext, SqlOperationError
from .connection.get_sql_connection import get_sql_connection, with_sql_connection
from .dml.load.load_df import load_df
from .dml.io.read_sql import read_sql, read_sql as read
from .dml.io.gp_cancel import gp_cancel_all_running_queries
from .dml.table.ch_create_table_as import ch_create_table_as
from .dml.table.ch_drop_table import ch_drop_table
from .dml.table.ch_full_table_move import ch_full_table_move
from .dml.table.create_table_from_sql import create_table_from_sql
from .dml.table import (
    build_gp_create_many_partitions_sqls,
    drop_many_partitions,
    gp_create_many_partitions,
    gp_vacuum,
)
from analytics_toolkit.general import time_print
from .dml.transfer.flow.api import transfer_table, transfer_table as transfer

_TIMED_PUBLIC_SQL_FUNCTION_NAMES = (
    "async_sql",
    "parallel_sql",
    "build_create_table_sql",
    "build_create_table_sqls",
    "build_gp_create_many_partitions_sqls",
    "ch_create_table_as",
    "ch_drop_table",
    "ch_full_table_move",
    "create_sql_table",
    "create_table_from_sql",
    "extract_ddl",
    "execute_read",
    "execute_sql",
    "format_support_matrix",
    "get_sql_connection",
    "drop_many_partitions",
    "gp_create_many_partitions",
    "gp_cancel_all_running_queries",
    "gp_vacuum",
    "load_df",
    "read_sql",
    "show_tables",
    "support_matrix_rows",
    "table_info",
    "transfer_table",
    "validate_connections",
    "with_sql_connection",
)

execute = execute_sql
read = read_sql
transfer = transfer_table

__all__ = [
    "async_sql",
    "parallel_sql",
    "BACKEND_CAPABILITIES",
    "ConnectionValidationResult",
    "BackendName",
    "ConnectionKey",
    "SqlTaskType",
    "SqlText",
    "TableName",
    "airflow_connection_config",
    "ch_create_table_as",
    "ch_drop_table",
    "ch_full_table_move",
    "execute",
    "execute_read",
    "execute_sql",
    "extract_ddl",
    "format_plan",
    "format_support_matrix",
    "build_gp_create_many_partitions_sqls",
    "build_create_table_sql",
    "build_create_table_sqls",
    "create_sql_table",
    "create_table_from_sql",
    "drop_many_partitions",
    "get_sql_connection",
    "gp_create_many_partitions",
    "gp_cancel_all_running_queries",
    "gp_vacuum",
    "load_df",
    "read",
    "read_sql",
    "show_tables",
    "SqlOperationMetadata",
    "SqlOperationResult",
    "SqlOperationContext",
    "SqlOperationError",
    "SqlPlan",
    "SqlStatement",
    "SqlTableInfo",
    "table_info",
    "time_print",
    "transfer",
    "transfer_table",
    "support_matrix_rows",
    "use_airflow_connections",
    "validate_connections",
    "with_sql_connection",
]

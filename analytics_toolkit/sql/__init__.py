from analytics_toolkit.general import (
    get_time_print_sink,
    set_time_print_sink,
    time_print,
)

from .connection.config import (
    ConnectionValidationResult,
    generate_dummy_connections,
    validate_connections,
)
from .connection.errors import (
    AmbiguousSqlReplaceError,
    ClickHouseClusterTopologyError,
    SqlOperationContext,
    SqlOperationError,
)
from .connection.get_sql_connection import get_sql_connection
from .connection.secret_setup import set_missing_secrets
from .core.capabilities import (
    BACKEND_CAPABILITIES,
)
from .core.types import BackendName, ConnectionKey, SqlTaskType, SqlText, TableName
from .ddl.api import (
    create_sql_table,
)
from .ddl.extract_ddl import extract_ddl
from .dml.empty_source import EmptySourceError, EmptySourcePolicy
from .dml.io.cancel_queries import cancel_queries
from .dml.io.execute_read import execute_read
from .dml.io.execute_safety import (
    AmbiguousSqlMutationError,
    ExecuteRetryPolicy,
    SqlBatchExecutionError,
    SqlBatchItemResult,
    SqlBatchItemStatus,
)
from .dml.io.execute_sql import execute_sql as execute
from .dml.io.read_sql import read_sql as read
from .dml.load.load_df import load_df
from .dml.table import (
    ch_reconfigure_table,
    drop_partitions,
    drop_tables,
    gp_analyze_partitioned_table,
    gp_create_partitions,
    gp_vacuum,
)
from .dml.transfer.flow.api import transfer_table as transfer
from .dml.transfer.staging import cleanup_stale_stage_tables
from .execution.labels import airflow_query_label
from .execution.plans import (
    SqlOperationMetadata,
    SqlOperationResult,
    SqlPlan,
    SqlStatement,
    format_plan,
)
from .metadata.show_queries import show_queries
from .metadata.show_tables import show_tables
from .metadata.table_info import SqlTableInfo, table_info
from .orchestration.async_sql import async_sql
from .orchestration.parallel_sql import parallel_sql

_TIMED_PUBLIC_SQL_FUNCTION_NAMES = (
    "async_sql",
    "parallel_sql",
    "cancel_queries",
    "ch_reconfigure_table",
    "create_sql_table",
    "extract_ddl",
    "execute_read",
    "execute",
    "drop_tables",
    "drop_partitions",
    "gp_analyze_partitioned_table",
    "gp_create_partitions",
    "gp_vacuum",
    "load_df",
    "read",
    "show_queries",
    "show_tables",
    "table_info",
    "cleanup_stale_stage_tables",
    "transfer",
    "validate_connections",
)

__all__ = [
    "BACKEND_CAPABILITIES",
    "AmbiguousSqlMutationError",
    "AmbiguousSqlReplaceError",
    "BackendName",
    "ClickHouseClusterTopologyError",
    "ConnectionKey",
    "ConnectionValidationResult",
    "EmptySourceError",
    "EmptySourcePolicy",
    "ExecuteRetryPolicy",
    "SqlBatchExecutionError",
    "SqlBatchItemResult",
    "SqlBatchItemStatus",
    "SqlOperationContext",
    "SqlOperationError",
    "SqlOperationMetadata",
    "SqlOperationResult",
    "SqlPlan",
    "SqlStatement",
    "SqlTableInfo",
    "SqlTaskType",
    "SqlText",
    "TableName",
    "async_sql",
    "cancel_queries",
    "ch_reconfigure_table",
    "cleanup_stale_stage_tables",
    "create_sql_table",
    "drop_partitions",
    "drop_tables",
    "execute",
    "execute_read",
    "extract_ddl",
    "format_plan",
    "generate_dummy_connections",
    "get_time_print_sink",
    "gp_analyze_partitioned_table",
    "gp_create_partitions",
    "gp_vacuum",
    "load_df",
    "parallel_sql",
    "read",
    "set_missing_secrets",
    "set_time_print_sink",
    "show_queries",
    "show_tables",
    "table_info",
    "time_print",
    "transfer",
    "validate_connections",
]

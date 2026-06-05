[SQL module index](index.md)

# Greenplum Operations

Greenplum-specific operations cover maintenance and table-shape defaults that
are not portable to every backend.

## Maintenance

Vacuum operations must run outside a transaction block. Use them for explicit
maintenance jobs where the caller controls when table cleanup or analyze work
happens.

Current-user query cancellation reads active backend sessions, excludes the
caller session, and issues cancellations with optional concurrency. Use it for
operational cleanup, not as normal flow control.

## Table Defaults

Created Greenplum tables default to append-only, column-oriented storage with
compression. Distribution is random unless a distribution key is provided.

Partitioned parent tables can be created with range partitioning options, while
child partitions are managed separately through the partitioning workflow.

[SQL module index](index.md)

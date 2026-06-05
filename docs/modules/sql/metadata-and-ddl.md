[SQL module index](index.md)

# Metadata and DDL

Metadata helpers answer questions about visible tables, resolved table names,
columns, optional row counts, and native DDL. They are useful before writes,
inside validation steps, and when documenting existing database objects. Use
[sql.show_tables](functions/show_tables.md) for listings,
[sql.table_info](functions/table_info.md) for one table, and
[sql.extract_ddl](functions/extract_ddl.md) for native DDL.

## Table Listings

Table listings return a consistent dataframe shape where possible: database,
schema, table name, row count, and human-readable size. Backend metadata is not
equally rich everywhere, so missing row counts or sizes should be expected.

Greenplum uses information schema plus relation metadata. Trino uses catalog
information schema and usually cannot expose portable row-count or table-size
values. ClickHouse uses local system metadata by default and can optionally
resolve distributed table shard statistics.

## Table Inspection

Table inspection is a lightweight existence and column check. Row counting is
opt-in because it runs a count query and may scan large tables.

## DDL Extraction

DDL extraction returns backend-native `CREATE TABLE` statements. Use it when
the exact existing database shape matters more than portable abstractions.

[SQL module index](index.md)

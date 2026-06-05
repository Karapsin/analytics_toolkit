# analytics_toolkit.sql

SQL utilities for reading, executing, loading, and transferring data across:

- Trino
- Greenplum
- ClickHouse

## Sections

- [Main Entry Points](main-entry-points.md)
- [Opt-In Write Controls](opt-in-write-controls.md)
- [Public API](public-api.md)
- [Configuration](configuration.md)
- [SQL Support Matrix](support-matrix.md)
- [Internal Layout](internal-layout.md)
- [Partition Management](partition-management.md)
- [Greenplum Maintenance](greenplum-maintenance.md)

SQL module docs describe general helpers first and backend-specific helpers
after them. Within each section, the helpers most likely to be used in normal
workflows appear first.

## Import Policy

Supported user-facing import style is `from analytics_toolkit import sql` or
`import analytics_toolkit.sql as sql`. Deep imports under
`analytics_toolkit.sql.*` are internal only and may change; call public helpers
through the `sql` facade. Do not restore removed root implementation paths.

# General Agent Instructions

Read this file for general helper code, tests, docs, API explanation, or
behavior investigation.

## General Module Contracts

- `time_print` prints timestamped messages and is re-exported through `analytics_toolkit.general` and `analytics_toolkit.sql`.
- `here()` first uses active Positron editor execution metadata when available,
  then preserves the existing `__main__.__file__`, caller stack, current working
  directory, and unique cwd-match fallback chain.
- `read_file()` raises `InvalidSqlInputError` for missing files and applies `str.format(**params_dict)` only when params are provided.
- Preserve the `analytics_toolkit.general.read_file.inspect` compatibility assignment; tests monkeypatch through that dotted path.

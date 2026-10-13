# Reusable test support

Keep a test's inputs and expected outcomes visible. Extract setup used by at
least two tests into the nearest domain `_support` package; put only genuinely
cross-domain helpers here. Pytest excludes `_support` from collection. Fixtures
belong in the nearest `conftest.py`. Common support must not import optional SDKs
or UI modules.

## Available patterns

- `files.write_json(path, value)` creates a temporary JSON fixture and returns
  its path; `files.read_json(path)` returns fresh data. This is fixture setup,
  not the implementation under test.
- `clock.FakeClock(value=0)` supports calling, `monotonic()`, `set(value)` and
  `sleep(seconds)`. Advance it explicitly instead of sleeping in timeout tests.
- `concurrency.ConcurrencyProbe(expected)` rendezvous the first real worker
  wave and records every call; use `with probe.worker()` and then
  `probe.assert_complete(calls=...)`. It detects insufficient parallelism,
  excess concurrency and unfinished workers, including partial final waves.
- `concurrency.wait_event` and `wait_event_async` bound worker-state waits;
  the async form leaves the UI/event loop responsive. `observe_call` observes
  a real primitive, optionally after an error or a matching result (such as a
  semaphore returning `False`). Release blocked
  workers in `finally` before executor/context teardown.
- `concurrency.release_on_completion` releases a slow fake after another real
  Future completes, so input-order tests force a different completion order.
- `source.CodeRules` and `forbidden_code` express architecture rules through
  identifiers, imports, expressions, call arguments and SQL literals. They
  ignore comments/docstrings and Python layout; these are ownership checks,
  not substitutes for behavioral coverage.
- SQL `ScriptedConnection` consumes ordered `SqlResponse` records with expected
  SQL, parameters, rows, columns and optional errors. `ConnectionSequence`
  supplies a finite sequence for retries. Call `assert_complete()` to detect
  missing operations. Unexpected calls fail, never connect to a database.
- SQL `assert_sql_equal` and `assert_sql_calls` require an explicit dialect and
  validate parsing before comparing tokens. They allow whitespace and keyword
  case changes plus optional final terminators; identifiers, literals,
  parameters, comments, statement order and call boundaries remain strict.
  Keep exact comparisons in formatting, comment, batching and driver-syntax
  tests. `strip_query_label` is only for fake dispatch, not SQL assertions.
- AB `metric_frame(groups, **metrics)` supplies sequential user IDs while the
  caller specifies groups and metric values. `sample_metrics_df()` returns the
  established sample afresh. Keep independent reference statistics in tests.
- Excel `report_frame(metric, values, **columns)` builds the standard two-group
  report input with explicit overrides. `report_workbook(path, **options)`
  closes the workbook even if an assertion fails; keep cell, format, sheet-order
  and spacing expectations in the test.
- Explorer's reusable sessions live in `tests.sql._support.explorer`; import
  them directly rather than importing a collected test module.
- Explorer metadata providers/coordinator stubs and bounded predicate polling
  live in `tests.sql._support.completion`.
- Explorer's area fixture supplies empty offline metadata for unattended UI
  discovery. Other SQL reads need an explicit fake; tests may override the
  metadata fake to exercise results and errors.
- SQL `tasks.patch_task_operations(monkeypatch, mode="async"|"parallel", ...)`
  installs named `read`, `execute`, `execute_read`, `load_df`, or `transfer`
  fakes. AB `parallel.patch_metric_runner` installs a dataframe task runner.
  Public orchestration behavior tests should use these seams and the facade.
- DataLens retains its SDK-local `MemoryDataLens`, typed resource factories and
  recipe fixtures. Shared JSON setup does not import its optional SDK.

## Refactor boundaries and case mapping

The offline-suite audit found useful consolidation in SQL I/O and orchestration,
Explorer sessions, AB input frames, Excel setup/ownership, general path tests,
DataLens recipe files, and agent/release/repository fixture setup. The existing
SQL formatting, `atk` export, architecture, statistical reference, DataLens
drift and workflow-security checks retain their precise contracts. No broad
snapshot normalization or automatic tolerance changes are applied.

Existing test names and parameter IDs are preserved except two aggregate input
checks expanded into independently named cases:

| Existing check | New parameter IDs |
| --- | --- |
| `tests/dates/functions.py::test_sanitize_date_accepts_date_and_datetime` | `date`, `datetime` |
| `tests/datetime/functions.py::test_format_datetime_accepts_supported_inputs_and_drops_microseconds` | `space`, `iso`, `date-string`, `date`, `microseconds` |

Integration tests and their scenario manifest are unchanged. Older general SQL
fakes retain their behavior; the stricter scripted driver is opt-in. Common
helpers have no new dependencies. Comparison/helper regression tests explicitly
exercise wrong SQL, parameters, call order, missing operations, state isolation
and resource cleanup. No production API changes are needed.

## Stable expectations

See [the stability audit and case map](stability.md) for the migration and
intentional exceptions. Assert observable results, errors and resource
ownership; keep operation order strict when it protects transactions, retries
or safety. Tests of exported internals and focused algorithms remain valid.
For a refactor-only change, identify the contract behind any changed behavior
expectation. Move patch-location maintenance into domain support.

Use Events/Barriers for worker ordering and a fake clock for elapsed-time
logic. The repository guard rejects new scheduling sleeps and direct
source-spelling inspection, while retaining documented real UI timer checks.
Keep independent numerical references and SQL/format/security contracts strict.
Dependency upgrades should exercise the same contract tests in the existing
minimum/latest and SDK environments, with no automatic tolerance widening.

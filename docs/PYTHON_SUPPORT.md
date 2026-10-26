[Documentation overview](README.md)

# Python Support Policy

`analytics-toolkit` supports a rolling window of the seven newest stable CPython
minor versions. When a new minor is released the window moves forward and the
oldest minor is dropped.

If any optional extra (`airflow`, `clickhouse-native`, `tui`, or `datalens`)
cannot yet be installed on the newest minor, the window is temporarily extended
to eight minors so the oldest minor is kept. Once every extra installs on the
newest minor, the window shrinks back to seven. The `all` extra follows the
intersection of the individual extras.

The `airflow` extra keeps its Airflow 2 (from 2.4) and Airflow 3 ranges. The
drift check resolves the extra exactly as declared in `pyproject.toml`, so the
extra supports a Python minor when any allowed Airflow release installs on it:
older minors keep Airflow 2 while newer minors use Airflow 3.

## Current Support

<!-- python-support-table -->
| Installation | Supported CPython minors |
| --- | --- |
| Core (`requires-python >=3.8,<3.15`) | 3.8-3.14 |
| `airflow` extra | 3.8-3.14 |
| `clickhouse-native` extra | 3.8-3.14 |
| `tui` extra | 3.8-3.14 |
| `datalens` extra | 3.10-3.14 |

Current window: 7 minors ending at 3.14; extras lagging the newest minor: none. Post-merge runtime validation covers 3.9-3.14.
<!-- /python-support-table -->

Python 3.8 remains in the published metadata as the historical package floor,
but runtime validation starts at Python 3.9.

### Initial policy evidence (2026-10-26)

The committed policy records the last validated window, ending at 3.14:

- A full-install probe (`python_support_drift probe`) on CPython 3.14.5 installed
  core and every extra (`airflow`, `clickhouse-native`, `tui`, `datalens`), so
  3.14 has no lagging extras and the window is seven minors (3.8-3.14).
- endoflife.date lists CPython 3.15 as released on 2026-10-09, and the newest
  Airflow (3.3.2) declares `requires-python !=3.15,>=3.10`.

By owner decision the initial window starts at the last validated minor (3.14).
Core dependencies do not yet install on CPython 3.15 (SciPy has no OpenBLAS
install path there and `numpy<2` publishes no 3.15 wheels), so the drift check
reports `WAITING FOR UPSTREAM SUPPORT` for 3.15 instead of failing. Once core
installs on 3.15, the check reports a stale window and the reviewed repair PR
moves it to 3.15, with eight minors while any extra (such as `airflow`) lags.

## Single Policy File

`release_routines/python_support.json` is the only place where the window is
decided. It records the newest supported minor, the window floor, the extras
that lag the newest minor, and extra-specific floors.
Repository tests and the compatibility workflow reject a policy whose floor does
not match the seven/eight-minor rule, and reject `pyproject.toml`
(`requires-python` and classifiers), the README `Depends` line, `tox.ini`
profiles, or this table when they disagree with the policy.

`python -m release_routines.lib.python_support sync` rewrites the generated
metadata from the policy. `tox.ini` profiles and minimum-dependency constraints
remain reviewed edits.

## Validation

- Everyday `tests.yml` fast checks run on Python 3.14 only.
- After every merge, `compatibility.yml` reads the policy and runs the full test
  suite on every runtime-validated minor plus the minimum-dependency profile.
- While an extra lags the newest minor, that minor's environment omits the extra.
  `ATK_WITHOUT_EXTRAS` then skips every test module that imports the extra's
  dependencies (directly or through test support modules), and the SQL Explorer
  renderer diagnostics are skipped; the core suite still runs on that minor.
- The same workflow runs a drift check against actual CPython releases and
  extra support.

## Drift Check

The `python-support-drift` job runs after merges to `dev` and `main`:

1. It reads stable CPython minors from endoflife.date, falling back to the
   python.org downloads API.
2. On the newest stable CPython it installs the core dependencies, and each extra
   together with them, into fresh disposable virtual environments. A full install
   (building any source distributions) is the support criterion, so an extra
   whose native dependencies cannot build does not count as supported.
3. It compares those facts with the policy.

The check fails with `STALE PYTHON SUPPORT WINDOW` when:

- a new CPython minor has been released, the core dependencies install on it,
  and the window has not moved (lagging extras extend the expected window to
  eight minors);
- a lagging extra now installs on the newest minor, so the window can shrink;
- an extra no longer installs on the newest minor, so the window must extend;
- the policy names a minor that has no stable release;
- the core dependencies no longer install on the policy's newest minor.

The message includes the expected newest minor, floor, lagging extras and
`requires-python` range.

A new CPython minor is only a policy obligation once the package can support
it. When a newer minor is released but the core dependencies cannot install on
it yet, the check passes (exit status 0) and reports
`WAITING FOR UPSTREAM SUPPORT: CPython 3.X released; core dependencies do not
install yet (<pip failure>)`. No policy PR could pass in that state, so no
repair is requested; the window stays unchanged until upstream packages ship
support and the check turns stale.

When release or package data cannot be fetched, the check fails separately with
`RELEASE DATA UNAVAILABLE` (exit status 3). The policy was not judged, so rerun
the workflow rather than changing the policy.

A stale-window failure on `dev` is repaired through the normal repair flow: a
reviewed PR updates the policy, runs `sync`, aligns `tox.ini` and constraints
profiles, and updates documentation. Nothing updates or merges the policy
automatically.

[Documentation overview](README.md)

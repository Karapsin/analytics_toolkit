# Dates Agent Instructions

Read this file for date helper code, tests, docs, API explanation, or behavior
investigation.

## Dates Contracts

- Date helpers accept ISO strings, `date`, or `datetime` values.
- The default return type is an ISO string; `output_string=False` returns midnight `datetime` values.
- Weekly and monthly sequences truncate start/end dates to the period start and emit warnings when truncation happens.
- `add_weeks` and `add_months` operate from the week/month start, not from the exact input day.

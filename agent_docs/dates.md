# Dates Agent Instructions

Read this file for date helper code, tests, docs, API explanation, or behavior
investigation.

## Dates Contracts

- Date helpers accept ISO strings, `date`, or `datetime` values.
- The default return type is an ISO string; `output_string=False` returns midnight `datetime` values.
- Weekly and monthly sequences truncate start/end dates to the period start and emit warnings when truncation happens.
- `add_weeks` and `add_months` operate from the week/month start, not from the exact input day.

## Date And Datetime Parity

- Keep `analytics_toolkit.dates` and `analytics_toolkit.datetime` mirrored where practical: when adding a public helper to one module, add the closest timestamp/date analog to the other module or document why no analog is appropriate.

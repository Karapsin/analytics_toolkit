SELECT
  toDateTime(event_date) AS "Date",
  stage AS "Stage",
  sum(acquisitions) AS "Acquisitions"
FROM __TABLE_ACQUISITION__
WHERE ('All' IN {{region}} OR region IN {{region}})
  AND event_date >= toDate({{interval_from}})
  AND event_date <= toDate({{interval_to}})
GROUP BY event_date, stage, stage_order
ORDER BY event_date, stage_order

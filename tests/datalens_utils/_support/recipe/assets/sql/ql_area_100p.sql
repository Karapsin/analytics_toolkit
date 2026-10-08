SELECT
  toDateTime(event_date) AS "Date",
  channel AS "Channel",
  sum(acquisitions) AS "Acquisitions"
FROM __TABLE_ACQUISITION__
WHERE ('All' IN {{region}} OR region IN {{region}})
  AND event_date >= toDate({{interval_from}})
  AND event_date <= toDate({{interval_to}})
  AND stage_order = 4
GROUP BY event_date, channel
ORDER BY event_date, channel

SELECT
  sum(revenue) AS "Revenue"
FROM __TABLE_RETAIL__
WHERE ('All' IN {{region}} OR region IN {{region}})
  AND event_date = toDate({{snapshot_date}})

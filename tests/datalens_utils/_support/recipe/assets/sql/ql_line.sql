SELECT
  toDateTime(event_date) AS "Date",
  sum(revenue) AS "Revenue",
  sum(profit) AS "Profit"
FROM __TABLE_RETAIL__
WHERE ('All' IN {{region}} OR region IN {{region}})
  AND event_date >= toDate({{interval_from}})
  AND event_date <= toDate({{interval_to}})
GROUP BY event_date
ORDER BY event_date

SELECT
  category AS "Category",
  channel AS "Channel",
  sum(revenue) AS "Revenue"
FROM __TABLE_RETAIL__
WHERE ('All' IN {{region}} OR region IN {{region}})
  AND event_date >= toDate({{interval_from}})
  AND event_date <= toDate({{interval_to}})
GROUP BY category, channel
ORDER BY category, channel

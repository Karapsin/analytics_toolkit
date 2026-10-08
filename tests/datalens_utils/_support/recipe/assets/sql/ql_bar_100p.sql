SELECT
  region AS "Region",
  channel AS "Channel",
  sum(deliveries) AS "Deliveries"
FROM __TABLE_DELIVERY__
WHERE ('All' IN {{region}} OR region IN {{region}})
  AND event_date >= toDate({{interval_from}})
  AND event_date <= toDate({{interval_to}})
GROUP BY region, channel
ORDER BY region, channel

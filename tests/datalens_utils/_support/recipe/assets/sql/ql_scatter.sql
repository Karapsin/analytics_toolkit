SELECT
  concat(region, ' · ', category) AS "Segment",
  sum(orders) AS "Orders",
  sum(revenue) AS "Revenue",
  sum(profit) AS "Profit"
FROM __TABLE_RETAIL__
WHERE ('All' IN {{region}} OR region IN {{region}})
  AND event_date >= toDate({{interval_from}})
  AND event_date <= toDate({{interval_to}})
  AND revenue >= {{min_revenue}}
GROUP BY region, category
ORDER BY "Revenue" DESC

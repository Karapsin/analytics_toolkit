SELECT
  region AS "Region",
  category AS "Category",
  sum(revenue) AS "Revenue"
FROM __TABLE_RETAIL__
WHERE ('All' IN {{region}} OR region IN {{region}})
  AND event_date >= toDate({{interval_from}})
  AND event_date <= toDate({{interval_to}})
GROUP BY region, category
ORDER BY "Revenue" DESC

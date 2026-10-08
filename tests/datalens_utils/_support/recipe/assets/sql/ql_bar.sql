SELECT
  route AS "Route",
  sum(deliveries) AS "Deliveries"
FROM __TABLE_DELIVERY__
WHERE ('All' IN {{region}} OR region IN {{region}})
  AND event_date >= toDate({{interval_from}})
  AND event_date <= toDate({{interval_to}})
GROUP BY route
ORDER BY "Deliveries" DESC

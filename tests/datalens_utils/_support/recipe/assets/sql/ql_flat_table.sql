SELECT
  region AS "Region",
  category AS "Category",
  channel AS "Channel",
  sum(orders) AS "Orders",
  toString(round(sum(revenue), 2)) AS "Revenue",
  toString(round(sum(profit), 2)) AS "Profit",
  toString(round(ifNull(sum(profit) / nullIf(sum(revenue), 0), 0), 4)) AS "Margin",
  toString(round(ifNull(sum(revenue) / nullIf(sum(orders), 0), 0), 2)) AS "Average Order Value",
  toString(round(sumIf(revenue, priority), 2)) AS "Priority Revenue",
  toString(round(sum(revenue) + sum(profit) * (sum(orders)), 2)) AS "Compound Score"
FROM __TABLE_RETAIL__
WHERE ('All' IN {{region}} OR region IN {{region}})
  AND event_date >= toDate({{interval_from}})
  AND event_date <= toDate({{interval_to}})
  AND ({{search}} = '' OR positionCaseInsensitiveUTF8(category, {{search}}) > 0)
  AND ({{only_priority}} != 'true' OR priority)
GROUP BY region, category, channel
ORDER BY sum(revenue) DESC

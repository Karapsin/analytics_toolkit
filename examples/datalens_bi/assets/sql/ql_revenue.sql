-- Deployed QL revenue query; region is an explicit multiselect parameter.
SELECT toString(toDate(event_date)) AS `Date`, sum(revenue) AS `Revenue`
FROM example.sales
WHERE ('All' IN {{region}} OR region IN {{region}})
GROUP BY toDate(event_date)
ORDER BY `Date`

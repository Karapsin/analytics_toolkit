-- Chart ql_revenue; selectors/controls ql_region; cleared, default and selected values.
SELECT toString(toDate(event_date)) AS `Date`, sum(revenue) AS `Revenue`
FROM @TABLE@
WHERE 1 = 1
-- SELECTOR_FILTERS
GROUP BY toString(toDate(event_date))
ORDER BY `Date`

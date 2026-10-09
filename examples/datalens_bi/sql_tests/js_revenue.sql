-- Chart js_revenue; selectors/controls js_selector_widget region control; cleared, default and selected values.
SELECT toString(toDate(event_date)) AS `Date`, sum(revenue) AS `Revenue`
FROM example.sales
WHERE 1 = 1
-- SELECTOR_FILTERS
GROUP BY toString(toDate(event_date))
ORDER BY `Date`

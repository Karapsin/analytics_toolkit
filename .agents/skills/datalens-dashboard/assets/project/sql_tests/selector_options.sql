-- Chart js_selector; controls region options, unfiltered for selection/reset.
SELECT DISTINCT toString(region) AS `Region`
FROM @TABLE@
ORDER BY `Region`

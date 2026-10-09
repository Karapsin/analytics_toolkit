-- Explicit source fields; adapt physical columns when adapting the source.
SELECT toDate(event_date) AS event_date, toString(region) AS region, toFloat64(revenue) AS revenue
FROM example.sales

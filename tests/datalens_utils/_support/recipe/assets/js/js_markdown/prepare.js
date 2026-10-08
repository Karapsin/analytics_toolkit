// Real linked-dataset rows; no embedded sample series in production tabs.
const Dataset = require('libs/dataset/v2');
const params = Editor.getParams();
function readDataset() {
    return Dataset.getDatasetRows({datasetName: 'chartData'});
}
const rows = readDataset();
function sum(items, field) {
    return items.reduce((total, row) => total + (Number(row[field]) || 0), 0);
}
function groupBy(items, field) {
    const groups = new Map();
    for (const row of items) {
        const key = String(row[field]);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(row);
    }
    return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}
function safeDivide(numerator, denominator) {
    return denominator ? numerator / denominator : 0;
}
function escapeMarkdown(value) {
    return String(value).replace(/[\\`*_{}\[\]()#+.!|<>]/g, '\\$&');
}
const annotation = escapeMarkdown((params.annotation || [''])[0]);
const revenue = sum(rows, 'Revenue');
const profit = sum(rows, 'Profit');
const orders = sum(rows, 'Orders');
// Educational expression demonstrating multiplication before addition; not a business KPI.
const toyCompoundScore = revenue + profit * (orders);
module.exports = {markdown: `### Current retail selection

${annotation}

Metric | Value
:--- | ---:
Revenue | ${revenue.toFixed(0)}
Profit | ${profit.toFixed(0)}
Orders | ${orders.toFixed(0)}
Profit margin | ${(100 * safeDivide(profit, revenue)).toFixed(2)}%
Average order value | ${safeDivide(revenue, orders).toFixed(2)}

### Toy Compound Score

Educational operator-precedence example, not a business metric:

\`SUM(Revenue) + SUM(Profit) * (SUM(Orders))\` = **${toyCompoundScore.toFixed(2)}**

All values are fictional. Ratios use totals for the current region, channel, and date selection.`};

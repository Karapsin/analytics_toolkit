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
// One dot per date × region, with ratios computed from total numerators.
const groups = new Map();
for (const row of rows) {
    const key = `${row.Date} · ${row.Region}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
}
const data = [...groups.entries()].map(([name, items]) => ({name, x: safeDivide(sum(items, 'Revenue'), sum(items, 'Orders')), y: 100 * safeDivide(sum(items, 'Profit'), sum(items, 'Revenue'))}));
module.exports = {series: {data: [{type: 'scatter', name: 'Region / day', data}]}, xAxis: {title: {text: 'Revenue / order'}}, yAxis: [{title: {text: 'Profit margin (%)'}}], legend: {enabled: false}};

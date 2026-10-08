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
const categories = [...new Set(rows.map(row => String(row.Category)))].sort();
const regions = [...new Set(rows.map(row => String(row.Region)))].sort();
const groups = new Map();
for (const row of rows) {
    const key = `${row.Category}|${row.Region}`;
    groups.set(key, (groups.get(key) || 0) + (Number(row.Revenue) || 0));
}
const data = categories.flatMap((category, x) => regions.map((region, y) => ({x, y, value: groups.get(`${category}|${region}`) || 0})));
module.exports = {series: {data: [{type: 'heatmap', name: 'Revenue', data}]}, xAxis: {type: 'category', categories}, yAxis: [{type: 'category', categories: regions}], legend: {enabled: false}};

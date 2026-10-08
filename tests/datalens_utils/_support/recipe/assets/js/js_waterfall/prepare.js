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
const revenue = sum(rows, 'Revenue');
const profit = sum(rows, 'Profit');
const data = [{x: 0, y: revenue}, {x: 1, y: -(revenue - profit)}, {x: 2, total: true}];
module.exports = {series: {data: [{type: 'waterfall', name: 'Profit bridge', data, positiveColor: '#3DAA6D', negativeColor: '#EA6758'}]}, xAxis: {type: 'category', categories: ['Revenue', 'Cost', 'Profit']}, yAxis: [{title: {text: 'Amount'}}], legend: {enabled: false}};

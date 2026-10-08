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
const data = groupBy(rows, 'Date').map(([date, dayRows]) => ({x: Date.parse(date), y0: sum(dayRows, 'Target Low'), y1: sum(dayRows, 'Target High')}));
const actual = groupBy(rows, 'Date').map(([date, dayRows]) => ({x: Date.parse(date), y: sum(dayRows, 'Revenue')}));
module.exports = {series: {data: [{type: 'area-range', name: 'Target band', data, color: '#7AA8F5', opacity: 0.35}, {type: 'line', name: 'Revenue', data: actual, color: '#1667D9'}]}, xAxis: {type: 'datetime'}, yAxis: [{title: {text: 'Revenue'}}], legend: {enabled: true}};

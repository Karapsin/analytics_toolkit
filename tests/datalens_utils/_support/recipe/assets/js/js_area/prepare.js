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
const days = [...new Set(rows.map(row => String(row.Date)))].sort();
const series = groupBy(rows, 'Channel').map(([name, items]) => {
    const daily = new Map(groupBy(items, 'Date').map(([date, dayRows]) => [date, sum(dayRows, 'Revenue')]));
    return {type: 'area', name, stacking: 'normal', data: days.map(date => ({x: Date.parse(date), y: daily.get(date) || 0}))};
});
module.exports = {series: {data: series}, xAxis: {type: 'datetime'}, yAxis: [{title: {text: 'Revenue'}}], legend: {enabled: true}};

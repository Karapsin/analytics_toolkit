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
const data = [];
for (const [category, items] of groupBy(rows, 'Category')) {
    const id = `category:${category}`;
    data.push({id, name: category});
    for (const [channel, channelRows] of groupBy(items, 'Channel')) {
        data.push({id: `${id}:${channel}`, parentId: id, name: channel, value: sum(channelRows, 'Revenue')});
    }
}
module.exports = {series: {data: [{type: 'treemap', name: 'Revenue', data}]}, legend: {enabled: false}};

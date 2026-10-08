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
const head = [{id: 'day', name: 'Date', type: 'text'}, {id: 'region', name: 'Region', type: 'text'}, {id: 'category', name: 'Category', type: 'text'}, {id: 'channel', name: 'Channel', type: 'text'}, {id: 'revenue', name: 'Revenue', type: 'number', formatter: {precision: 0}}, {id: 'margin', name: 'Profit margin (%)', type: 'number', formatter: {precision: 2}}, {id: 'priority', name: 'Priority', type: 'text'}];
const displayRows = rows.map(row => ({cells: [{value: String(row.Date)}, {value: row.Region}, {value: row.Category}, {value: row.Channel}, {value: Number(row.Revenue)}, {value: 100 * safeDivide(Number(row.Profit), Number(row.Revenue))}, {value: ['true', '1'].includes(String(row.Priority)) ? 'Yes' : 'No'}]}));
module.exports = {head, rows: displayRows, footer: []};

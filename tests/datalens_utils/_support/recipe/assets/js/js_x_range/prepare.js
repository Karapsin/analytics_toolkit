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
// Retain complete latest-day tasks rather than arbitrary truncation across dates.
const latest = rows.reduce((value, row) => String(row.Date) > value ? String(row.Date) : value, '');
const tasks = rows.filter(row => String(row.Date) === latest).sort((a, b) => String(a.Route).localeCompare(String(b.Route)) || Number(a['Point Order']) - Number(b['Point Order']));
const categories = tasks.map(row => `${row.Region} · ${row.Route} · ${row['Point Order']}`);
function timestamp(value) {
    const text = String(value).replace(' ', 'T');
    // CH DateTime without a zone is UTC in this example; preserve explicit zones.
    return Date.parse(/(?:[zZ]|[+-]\d{2}:\d{2})$/.test(text) ? text : `${text}Z`);
}
const data = tasks.map((row, y) => ({x0: timestamp(row['Start Time']), x1: timestamp(row['End Time']), y, label: `${row.Deliveries} deliveries`}));
module.exports = {series: {data: [{type: 'x-range', name: latest ? `Tasks on ${latest}` : 'No tasks', data}]}, xAxis: {type: 'datetime'}, yAxis: [{type: 'category', categories}], legend: {enabled: false}};

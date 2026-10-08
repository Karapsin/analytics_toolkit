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
// Each region has its own 100% category mix; scale maxima remain comparable.
const categories = [...new Set(rows.map(row => String(row.Category)))].sort();
const series = groupBy(rows, 'Region').map(([name, items]) => ({type: 'radar', name, categories: categories.map(key => ({key, maxValue: 100})), data: categories.map(category => ({value: 100 * safeDivide(sum(items.filter(row => row.Category === category), 'Revenue'), sum(items, 'Revenue'))}))}));
module.exports = {series: {data: series}, legend: {enabled: true}};

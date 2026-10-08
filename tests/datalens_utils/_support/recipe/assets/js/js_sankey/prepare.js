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
// Ordered stage counts describe a single fictional cohort flow.
// Intermediate node outflow = progression to next stage + stage-specific drop-off.
const stages = groupBy(rows, 'Stage').map(([name, items]) => ({
    name, value: sum(items, 'Acquisitions'), order: Number(items[0]['Stage Order']),
})).sort((a, b) => a.order - b.order);
const nodes = [];
for (let index = 0; index < stages.length; index += 1) {
    const current = stages[index];
    const next = stages[index + 1];
    if (!next) {
        nodes.push({name: current.name, links: []});
        continue;
    }
    if (next.value > current.value) {
        throw new Error('Sankey cohort counts must not increase across ordered stages.');
    }
    const links = [];
    if (next.value > 0) links.push({name: next.name, value: next.value});
    const lost = current.value - next.value;
    if (lost > 0) {
        const dropoff = `Drop-off after ${current.name}`;
        links.push({name: dropoff, value: lost});
        nodes.push({name: dropoff, links: []});
    }
    nodes.push({name: current.name, links});
}
module.exports = {
    series: {data: nodes.length ? [{type: 'sankey', name: 'Acquisition progression and drop-off', data: nodes}] : []},
    legend: {enabled: false},
};

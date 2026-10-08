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
const cards = groupBy(rows, 'Region').map(([region, items]) => ({region, revenue: sum(items, 'Revenue'), orders: sum(items, 'Orders'), margin: 100 * safeDivide(sum(items, 'Profit'), sum(items, 'Revenue'))}));
const day = (params.snapshot_date || ['2026-09-30'])[0];
module.exports = {render: Editor.wrapFn({
    fn: function(options, data, day) {
        return Editor.generateHtml({tag: 'div', style: {padding: '16px', display: 'flex', 'flex-direction': 'column', gap: '12px'}, content: [
            {tag: 'h3', content: `Snapshot · ${day}`},
            {tag: 'div', style: {display: 'flex', gap: '12px', 'flex-wrap': 'wrap'}, content: data.map(card => ({tag: 'div', style: {padding: '16px', border: '1px solid var(--g-color-line-generic)', 'border-radius': '12px', flex: '1', 'min-width': '130px'}, content: [
                {tag: 'strong', content: card.region},
                {tag: 'p', content: `Revenue: ${card.revenue.toFixed(0)}`},
                {tag: 'p', content: `Orders: ${card.orders.toFixed(0)}`},
                {tag: 'p', content: `Profit margin: ${card.margin.toFixed(1)}%`},
            ]}))},
            {tag: 'small', content: 'Snapshot date uses a separate receiver from the interval charts.'},
        ]});
    }, args: [cards, day],
})};

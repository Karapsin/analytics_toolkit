// Dataset query conditions use the public libs/dataset/v2 contract.
const {buildSource} = require('libs/dataset/v2');
const params = Editor.getParams();
function values(name) {
    const value = params[name] || [];
    return (Array.isArray(value) ? value : [value]).map(String).filter(Boolean);
}
const where = [];
for (const [param, column] of [['region', 'Region'], ['channel', 'Channel']]) {
    const selected = values(param).filter(value => value !== 'All');
    if (selected.length) where.push({column, operation: 'IN', values: selected});
}
const from = values('interval_from')[0] || '2026-07-01';
const to = values('interval_to')[0] || '2026-09-30';
if (from > to) throw new Error('Interval start must not be after interval end.');
where.push({column: 'Date', operation: 'BETWEEN', values: [from, to]});
module.exports = {chartData: buildSource({
    datasetId: Editor.getId('acquisition'),
    columns: ["Row ID", "Date", "Region", "Channel", "Stage", "Stage Order", "Acquisitions", "Cost"],
    where,
    // Row ID retains the row grain when aggregated dataset measures are selected.
    limit: 10000,
    order_by: [{column: 'Date', direction: 'ASC'}, {column: 'Row ID', direction: 'ASC'}],
})};

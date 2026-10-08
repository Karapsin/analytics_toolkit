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
const minRevenue = values('min_revenue')[0] || '0';
if (!Number.isFinite(Number(minRevenue)) || Number(minRevenue) < 0) {
    throw new Error('Minimum revenue must be a nonnegative number.');
}
where.push({column: 'Revenue', operation: 'GTE', values: [minRevenue]});
const search = values('search')[0] || '';
if (search) where.push({column: 'Category', operation: 'ICONTAINS', values: [search]});
if (values('only_priority')[0] === 'true') {
    where.push({column: 'Priority', operation: 'EQ', values: ['true']});
}
module.exports = {chartData: buildSource({
    datasetId: Editor.getId('retail'),
    columns: ["Row ID", "Date", "Region", "Channel", "Category", "Orders", "Revenue", "Profit", "Target Low", "Target High", "Priority"],
    where,
    // Row ID retains the row grain when aggregated dataset measures are selected.
    limit: 10000,
    order_by: [{column: 'Date', direction: 'ASC'}, {column: 'Row ID', direction: 'ASC'}],
})};

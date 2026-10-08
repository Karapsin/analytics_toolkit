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
const day = values('snapshot_date')[0] || '2026-09-30';
where.push({column: 'Date', operation: 'EQ', values: [day]});
module.exports = {chartData: buildSource({
    datasetId: Editor.getId('retail'),
    columns: ["Row ID", "Date", "Region", "Channel", "Category", "Orders", "Revenue", "Profit", "Target Low", "Target High", "Priority"],
    where,
    limit: 10000,
    order_by: [{column: 'Row ID', direction: 'ASC'}],
})};

const {buildSource} = require('libs/dataset/v2');
const params = Editor.getParams();
const region = (params.region || []).map(String).filter(value => value && value !== 'All');
module.exports = {chartData: buildSource({datasetId: Editor.getId('retail'), columns: ['Date', 'Revenue'],
    where: region.length ? [{column: 'Region', operation: 'IN', values: region}] : [],
    order_by: [{column: 'Date', direction: 'ASC'}], limit: 10000})};

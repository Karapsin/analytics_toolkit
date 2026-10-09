const Dataset = require('libs/dataset/v2');
const rows = Dataset.getDatasetRows({datasetName: 'chartData'});
module.exports = {series: {data: [{type: 'line', name: 'Revenue',
    data: rows.map(row => ({x: Date.parse(row.Date), y: Number(row.Revenue)}))}]},
    xAxis: {type: 'datetime'}, yAxis: [{title: {text: 'Revenue'}}]};

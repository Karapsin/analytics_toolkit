const Dataset = require('libs/dataset/v2');
const options = Dataset.getDatasetRows({datasetName: 'selectorOptions'});
const values = [...new Set(options.map(row => String(row.Region)))].sort();
module.exports = {controls: [{type: 'select', param: 'region', label: 'Regions', multiselect: true,
    content: [{title: 'All', value: 'All'}, ...values.map(value => ({title: value, value}))], updateOnChange: true}]};

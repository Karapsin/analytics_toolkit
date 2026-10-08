const Dataset = require('libs/dataset/v2');
const params = Editor.getParams();
const options = Dataset.getDatasetRows({datasetName: 'selectorOptions'});
function content(field) {
    return [{title: 'All', value: 'All'}, ...[...new Set(options.map(row => String(row[field])))].sort().map(value => ({title: value, value}))];
}
module.exports = {controls: [
    {type: 'select', param: 'region', label: 'Regions', content: content('Region'), multiselect: true, searchable: true, width: '180px', updateOnChange: true},
    {type: 'select', param: 'channel', label: 'Channel', content: content('Channel'), multiselect: false, searchable: true, width: '220px', updateOnChange: true},
    {type: 'range-datepicker', paramFrom: 'interval_from', paramTo: 'interval_to', label: 'Period', minDate: '2026-07-01', maxDate: '2026-09-30', width: '260px', updateOnChange: true},
    {type: 'datepicker', param: 'snapshot_date', label: 'Card snapshot', minDate: '2026-07-01', maxDate: '2026-09-30', width: '180px', updateOnChange: true},
    {type: 'line-break'},
    {type: 'input', param: 'min_revenue', label: 'Table minimum revenue', placeholder: '0', width: '180px', updateOnChange: true},
    {type: 'input', param: 'search', label: 'Table category contains', placeholder: 'e.g. Home', width: '220px', updateOnChange: true},
    {type: 'checkbox', param: 'only_priority', label: 'Table: priority only', updateOnChange: true},
    {type: 'textarea', param: 'annotation', label: 'Markdown annotation', width: '300px', updateOnChange: true},
    {type: 'line-break'},
    {type: 'button', param: 'apply_preset', label: 'September · North · Online', onClick: {action: 'setParams', mode: 'merge', args: {region: ['North'], channel: ['Online'], interval_from: ['2026-09-01'], interval_to: ['2026-09-30'], snapshot_date: ['2026-09-30'], min_revenue: ['0'], search: [''], only_priority: ['false']}}, updateOnChange: true},
    {type: 'button', param: 'reset_filters', label: 'Reset all', theme: 'normal', onClick: {action: 'setInitialParams'}, updateOnChange: true},
]};

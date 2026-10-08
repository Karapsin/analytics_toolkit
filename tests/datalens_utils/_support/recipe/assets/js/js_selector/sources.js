const {buildSource} = require('libs/dataset/v2');
// Unfiltered option source keeps values available after selection and reset.
module.exports = {selectorOptions: buildSource({datasetId: Editor.getId('retail'), columns: ['Region', 'Channel'], limit: 100, ui: true})};

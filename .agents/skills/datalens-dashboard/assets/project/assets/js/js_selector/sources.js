const {buildSource} = require('libs/dataset/v2');
module.exports = {selectorOptions: buildSource({datasetId: Editor.getId('retail'), columns: ['Region'], limit: 10000, ui: true})};

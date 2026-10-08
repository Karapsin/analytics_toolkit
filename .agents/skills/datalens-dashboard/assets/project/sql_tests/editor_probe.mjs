// Evaluate published Editor tabs with explicit API rows; never opens a browser.
import fs from 'node:fs';
import vm from 'node:vm';

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let params;
const requests = {};
const Dataset = {
    buildSource: request => request,
    getDatasetRows: ({datasetName}) => input.rows?.[datasetName] || [],
};
const Editor = {
    getParams: () => params,
    getId: alias => JSON.parse(input.tabs.meta).links[alias],
    getLoadedData: () => input.rows || {},
    wrapFn: value => value,
    generateHtml: value => value,
};
function run(tab) {
    const context = {module: {exports: {}}, Editor, require: name => {
        if (name !== 'libs/dataset/v2') throw new Error(`Unsupported Editor dependency ${name}`);
        return Dataset;
    }};
    vm.runInNewContext(input.tabs[tab], context, {filename: `${input.key}/${tab}`, timeout: 1000});
    return context.module.exports;
}
params = {...run('params'), ...(input.overrides || {})};
Object.assign(requests, run('sources'));
const result = {params, requests};
if (input.evaluate) {
    result.controls = run('controls').controls;
    if (input.tabs.prepare) {
        const prepared = run('prepare');
        result.values = prepared.series.data[0].data.map(point => ({Date: new Date(point.x).toISOString().slice(0, 10), Revenue: point.y}));
    }
}
process.stdout.write(JSON.stringify(result));

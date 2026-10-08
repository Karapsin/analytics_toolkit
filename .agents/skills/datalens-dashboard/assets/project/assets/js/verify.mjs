// Synthetic protocol checks; no data requests or browser automation.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {spawnSync} from 'node:child_process';

function probe(key, rows, overrides = {}) {
    const folder = key === 'js_selector' ? 'selectors/editor/selector' : 'charts/editor/gravity_charts/line';
    const definition = JSON.parse(fs.readFileSync(`configs/DL objects/${folder}/${key}.json`, 'utf8'));
    const tabs = Object.fromEntries(Object.entries(definition.scripts).map(([tab, path]) => [tab, fs.readFileSync(path, 'utf8')]));
    tabs.meta = JSON.stringify({links: {retail: 'offline-dataset'}});
    const result = spawnSync(process.execPath, ['sql_tests/editor_probe.mjs'], {encoding: 'utf8',
        input: JSON.stringify({key, tabs, rows, overrides, evaluate: true})});
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(result.stdout);
}
const result = probe('js_revenue', {chartData: [{Date: '2026-01-01', Revenue: 100}, {Date: '2026-01-02', Revenue: 300}]}, {region: ['North', 'South']});
assert.deepEqual(result.values, [{Date: '2026-01-01', Revenue: 100}, {Date: '2026-01-02', Revenue: 300}]);
assert.deepEqual(result.requests.chartData.where, [{column: 'Region', operation: 'IN', values: ['North', 'South']}]);
assert.deepEqual(probe('js_revenue', {chartData: []}).values, []);
assert.deepEqual(probe('js_revenue', {chartData: []}).requests.chartData.where, []);
const selector = probe('js_selector', {selectorOptions: [{Region: 'South'}, {Region: 'North'}, {Region: 'North'}]});
assert.deepEqual(selector.controls[0].content.map(option => option.value), ['All', 'North', 'South']);
assert.equal(selector.controls[0].param, 'region');
assert.equal(selector.controls[0].multiselect, true);
console.log('Editor fixture checks passed');

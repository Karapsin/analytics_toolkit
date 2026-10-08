// Offline protocol and semantic checks. Does not contact DataLens or read credentials.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

const assetRoot = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(assetRoot, '../..');
const fixtures = JSON.parse(fs.readFileSync(path.join(assetRoot, 'fixtures.json'), 'utf8'));
function filesBelow(folder) {
    return fs.readdirSync(folder, {withFileTypes: true}).flatMap(entry => entry.isDirectory()
        ? filesBelow(path.join(folder, entry.name)) : [path.join(folder, entry.name)]);
}
const definitions = [
    ...filesBelow(path.join(projectRoot, 'configs/DL objects/charts/editor')),
    ...filesBelow(path.join(projectRoot, 'configs/DL objects/selectors/editor')),
].filter(file => file.endsWith('.json')).map(file => JSON.parse(fs.readFileSync(file, 'utf8')));
const byKey = new Map(definitions.map(definition => [definition.key, definition]));
const sourceDatasets = JSON.parse(fs.readFileSync(path.join(projectRoot, 'configs/DL objects/datasets.json'), 'utf8'));
function matches(row, filter) {
    const value = row[filter.column];
    const [a, b] = filter.values;
    switch (filter.operation) {
        case 'IN': return filter.values.includes(String(value));
        case 'BETWEEN': return String(value) >= a && String(value) <= b;
        case 'EQ': return String(value) === a;
        case 'GTE': return Number(value) >= Number(a);
        case 'ICONTAINS': return String(value).toLowerCase().includes(a.toLowerCase());
        default: throw new Error(`Unexpected filter operation: ${filter.operation}`);
    }
}
function execute(definition, overrides = {}) {
    let params = {};
    let loadedRows = [];
    let sourceName = '';
    const builtSources = [];
    const Editor = {
        getParams: () => params,
        getId: alias => {
            assert.ok(definition.links[alias], `Missing linked alias ${alias}`);
            return definition.links[alias];
        },
        getLoadedData: () => ({[sourceName]: loadedRows}),
        wrapFn: configuration => configuration,
        generateHtml: value => value,
    };
    const Dataset = {
        buildSource: request => {
            const role = request.datasetId;
            assert.ok(fixtures[role], `Unknown dataset role ${role}`);
            const knownFields = new Set([
                ...Object.values(sourceDatasets[role].fields).map(field => field.title),
                ...Object.keys(sourceDatasets[role].calculations || {}),
                ...(sourceDatasets[role].measures || []).map(field => field.title),
            ]);
            for (const column of request.columns) assert.ok(knownFields.has(column), `Unknown column ${role}:${column}`);
            for (const filter of request.where || []) {
                assert.ok(knownFields.has(filter.column), `Unknown filter ${role}:${filter.column}`);
                assert.ok(filter.values.every(value => typeof value === 'string'));
            }
            assert.ok(request.limit >= fixtures[role].length);
            builtSources.push(request);
            return request;
        },
        getDatasetRows: ({datasetName}) => {
            assert.equal(datasetName, sourceName, 'Prepare must consume the requested source');
            return loadedRows;
        },
    };
    const run = filename => {
        const context = {module: {exports: {}}, Editor, require: name => {
            assert.equal(name, 'libs/dataset/v2', 'Only the documented dataset module is required');
            return Dataset;
        }};
        vm.runInNewContext(fs.readFileSync(path.join(projectRoot, filename), 'utf8'), context, {filename, timeout: 1000});
        return context.module.exports;
    };
    params = {...run(definition.scripts.params), ...overrides};
    assert.ok(Object.values(params).every(value => Array.isArray(value) && value.every(v => typeof v === 'string')));
    const sourceResults = run(definition.scripts.sources);
    [sourceName] = Object.keys(sourceResults);
    const request = sourceResults[sourceName];
    loadedRows = fixtures[request.datasetId].filter(row => (request.where || []).every(filter => matches(row, filter)));
    if (request.order_by) loadedRows.sort((a, b) => {
        for (const order of request.order_by) {
            const compared = String(a[order.column]).localeCompare(String(b[order.column]));
            if (compared) return order.direction === 'DESC' ? -compared : compared;
        }
        return 0;
    });
    const controls = run(definition.scripts.controls);
    const prepared = definition.scripts.prepare ? run(definition.scripts.prepare) : undefined;
    if (definition.scripts.config) run(definition.scripts.config);
    return {prepared, controls, loadedRows, builtSources, params, Editor};
}

assert.equal(definitions.length, 19);
assert.equal(new Set(definitions.map(value => value.key)).size, 19);
const expectedSeries = new Set(['area', 'area-range', 'bar-x', 'bar-y', 'line', 'pie', 'scatter', 'treemap', 'waterfall', 'funnel', 'heatmap', 'radar', 'x-range', 'sankey']);
assert.deepEqual(new Set(definitions.filter(d => d.type === 'gravity_charts').map(d => d.series_type)), expectedSeries);
assert.equal(definitions.filter(d => d.type === 'gravity_charts').length, 15);

for (const definition of definitions) {
    for (const scenario of [{}, {region: ['North'], channel: ['Online'], interval_from: ['2026-09-01']}, {region: ['Nowhere']}]) {
        const result = execute(definition, scenario);
        if (definition.type === 'gravity_charts') {
            assert.ok(Array.isArray(result.prepared.series.data));
            for (const series of result.prepared.series.data) {
                assert.ok(expectedSeries.has(series.type));
                assert.ok(Array.isArray(series.data));
                for (const point of series.data) for (const [field, value] of Object.entries(point)) {
                    if (['x', 'y', 'x0', 'x1', 'y0', 'y1', 'value'].includes(field) && value !== undefined) {
                        assert.ok(Number.isFinite(value), `${definition.key}: invalid ${field}`);
                    }
                    if (series.type === 'area-range') assert.ok(point.y0 <= point.y1);
                    if (series.type === 'x-range') assert.ok(point.x0 <= point.x1);
                }
            }
        } else if (definition.type === 'advanced_chart') {
            // Execute the browser render with only its explicit serializable args.
            const {fn, args} = result.prepared.render;
            const render = vm.runInNewContext(`(${fn.toString()})`, {Editor: result.Editor});
            assert.equal(render({width: 900, height: 400}, ...args).tag, 'div');
        } else if (definition.type === 'table') assert.equal(result.prepared.rows.length, result.loadedRows.length);
        else if (definition.type === 'markdown') assert.equal(typeof result.prepared.markdown, 'string');
    }
}

const selected = execute(byKey.get('js_markdown'), {region: ['North'], channel: ['Online'], interval_from: ['2026-09-01']});
assert.equal(selected.loadedRows.length, 2);
assert.ok(selected.prepared.markdown.includes('Revenue | 300'));
assert.ok(selected.prepared.markdown.includes('Profit margin | 12.67%'));
assert.ok(selected.prepared.markdown.includes('Toy Compound Score'));
assert.ok(selected.prepared.markdown.includes('= **1174.00**'));
const sankey = execute(byKey.get('js_sankey'), {region: ['North'], channel: ['Online'], interval_from: ['2026-09-01']});
const nodes = sankey.prepared.series.data[0].data;
const nodeNames = new Set(nodes.map(node => node.name));
assert.equal(nodeNames.size, nodes.length, 'Sankey node names must be unique');
for (const node of nodes) for (const link of node.links) {
    assert.ok(nodeNames.has(link.name), 'Every Sankey link resolves a node');
    assert.ok(Number.isFinite(link.value) && link.value > 0);
}
const visited = nodes.find(node => node.name === 'Visited');
assert.equal(visited.links.reduce((total, link) => total + link.value, 0), 1020, 'Progression plus drop-off conserves the cohort');
assert.equal(visited.links.find(link => link.name === 'Engaged').value, 520);
const increasing = fixtures.acquisition.find(row => row.Date === '2026-09-30' && row.Region === 'North' && row.Stage === 'Engaged');
const originalAcquisitions = increasing.Acquisitions;
try {
    increasing.Acquisitions = 2000;
    assert.throws(() => execute(byKey.get('js_sankey'), {region: ['North'], interval_from: ['2026-09-01']}), /must not increase/);
} finally {
    increasing.Acquisitions = originalAcquisitions;
}
const line = execute(byKey.get('js_line'), {region: ['North'], channel: ['Online'], interval_from: ['2026-09-01']});
assert.ok(Math.abs(line.prepared.series.data[0].data[0].y - 100 * 38 / 300) < 1e-9);
const filtered = execute(byKey.get('js_table'), {region: ['North'], channel: ['Online'], search: ['home'], min_revenue: ['130'], only_priority: ['true']});
assert.equal(filtered.loadedRows.length, 1);
assert.equal(filtered.prepared.rows[0].cells[4].value, 140);
assert.throws(() => execute(byKey.get('js_table'), {min_revenue: ['not a number']}), /Minimum revenue/);
assert.throws(() => execute(byKey.get('js_area'), {interval_from: ['2026-10-01'], interval_to: ['2026-07-01']}), /Interval start/);
const snapshot = execute(byKey.get('js_advanced'), {snapshot_date: ['2026-07-01'], interval_from: ['2026-09-01']});
assert.equal(snapshot.loadedRows.length, 8, 'Snapshot uses its own date, not interval controls');
const selector = execute(byKey.get('js_selector'));
assert.deepEqual(new Set(selector.controls.controls.map(control => control.type)), new Set(['input', 'textarea', 'datepicker', 'range-datepicker', 'select', 'checkbox', 'line-break', 'button']));
assert.ok(selector.controls.controls.find(control => control.param === 'region').content.some(option => option.value === 'North'));
const preset = selector.controls.controls.find(control => control.param === 'apply_preset').onClick;
const reset = selector.controls.controls.find(control => control.param === 'reset_filters').onClick;
assert.equal(preset.action, 'setParams');
assert.equal(reset.action, 'setInitialParams');
assert.equal(execute(byKey.get('js_markdown'), preset.args).loadedRows.length, 2);
const escaped = execute(byKey.get('js_markdown'), {annotation: ['<script>|**hi**']});
assert.ok(escaped.prepared.markdown.includes('\\<script\\>\\|\\*\\*hi\\*\\*'));
console.log('Editor offline checks passed: 18 visual charts, 1 selector, 14 series types + donut, 8 controls, source filters, formulas, empty selections, presets, and snapshot isolation.');

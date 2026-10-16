// Optional offline Node runtime checks. No browser layout or external requests.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
new vm.Script(script); // Compile the complete inline program, not only test snippets.
function source(name) {
    const start = script.indexOf('function ' + name + '(');
    assert(start >= 0, name);
    const end = script.indexOf('\n}\n', start);
    assert(end >= 0, name);
    return script.slice(start, end + 3);
}
class Element {
    constructor(tag = 'div') {
        this.tagName = tag; this.children = []; this.dataset = {}; this.attrs = {};
        this.style = {}; this.listeners = {}; this._text = ''; this.classes = new Set();
        this.classList = {add: n => this.classes.add(n), remove: n => this.classes.delete(n), contains: n => this.classes.has(n)};
    }
    appendChild(child) { this.children.push(child); return child; }
    replaceChildren() {
        if (this.contains(context.document.activeElement)) context.document.activeElement = context.document.body;
        this.children = []; this._text = '';
    }
    contains(element) { return this === element || this.children.some(c => c.contains(element)); }
    querySelectorAll() { return this.children.filter(c => c.tagName === 'button' && c.dataset.focusKey); }
    set textContent(value) { this.children = []; this._text = String(value); }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(' '); }
    setAttribute(key, value) { this.attrs[key] = value; }
    addEventListener(event, fn) { this.listeners[event] = fn; }
    focus() { this.focused = true; context.document.activeElement = this; }
    scrollIntoView() { this.scrolled = true; }
}
const elements = Object.fromEntries(['hudStats', 'hudFeedback', 'drawer', 'drawerToggle', 'detail', 'detailBody', 'detailHeader', 'detailClose', 'alertBanner', 'feedCount', 'lastUpdate'].map(id => ['#' + id, new Element()]));
const heading = new Element();
heading.classes.add('collapsed');
heading.nextElementSibling = new Element();
heading.nextElementSibling.style.display = 'none';
const cyberSelector = '#drawerBody [data-section="cyber"]';
elements[cyberSelector] = heading;
const requests = [];
let plotted = 0;
const context = vm.createContext({
    console, URL, Date, document: {body: new Element('body'), activeElement: null, createElement: tag => new Element(tag), querySelector: s => elements[s] || null},
    $: s => { assert(elements[s], s); return elements[s]; },
    fitHeader: () => {}, fmtBigPlain: String, latestData: {}, latestStaticData: null,
    INFRA_SNAPSHOT_KEYS: [], _bannerAlerts: [], _bannerDigest: null,
    mapLayers: {}, layerState: {}, localStorage: {setItem: () => {}},
    map: {fitBounds: () => plotted++},
    L: {latLngBounds: () => ({points: [], extend(p) { this.points.push(p); }, isValid() { return this.points.length > 0; }, getNorthEast: () => ({lat: 2}), getSouthWest: () => ({lat: 1})})},
    fetch: url => { requests.push(url); throw new Error('Network is prohibited'); }
});
for (const name of ['rememberControlFocus', 'alertControlKey', 'updateHudStats', 'activateHudAction', 'updateAlertBanner', 'showAlertDetail', '_mergeStaticData']) vm.runInContext(source(name), context);
context.updateHudStats({military_flights: {count: 2}, cyber_threats: {threats: [{indicator: '<img onerror=bad>'}]}, displacement: {year: 2025, global_totals: {grand_total: 42}}, population_exposure: {exposed_city_count: 1, total_exposed_population_formatted: '1M'}});
let pills = elements['#hudStats'].children;
assert(pills.some(p => p.tagName === 'button' && p.type === 'button' && p.dataset.hudTarget === 'military'));
assert(pills.some(p => p.tagName === 'span' && p.textContent.includes('UNHCR 2025') && p.title.includes('not a live')));
assert(pills.some(p => p.dataset.hudTarget === 'exposure' && p.title.includes('not actual victims')));
assert(pills.every(p => !('innerHTML' in p)));
const cyber = pills.find(p => p.dataset.hudTarget === 'cyber');
elements['#drawer'].classes.add('closed');
context.activateHudAction(cyber.dataset.hudAction, cyber.dataset.hudTarget);
assert(!elements['#drawer'].classes.has('closed'));
assert(heading.scrolled && heading.focused);
assert(!heading.classList.contains('collapsed'));
assert.equal(heading.nextElementSibling.style.display, '');
delete elements[cyberSelector];
context.activateHudAction('drawer', 'cyber');
assert(elements['#hudFeedback'].textContent.includes('unavailable'));
context.mapLayers.military = {eachLayer: include => include({getLatLng: () => [1, 2]}), addTo: () => {}};
context.activateHudAction('map', 'military');
assert.equal(plotted, 1); // Ordinary layerGroup, without getBounds().
context.mapLayers.military = {eachLayer: () => {}};
context.activateHudAction('map', 'military');
assert(elements['#hudFeedback'].textContent.includes('No plotted data'));
const clean = context._mergeStaticData({webcams: {}, migration: {}, signal_convergence: {}, population_exposure: {kept: true}});
assert.deepEqual(Object.keys(clean), ['population_exposure']);

context.updateAlertBanner({timestamp: '2026-10-16', alerts: [{domain: 'military', priority: 'high', message: 'Military surge', value: 7, surges: [{region: 'test_region', country: 'XX', current: 6, baseline: 2, surge_ratio: 3, severity: 'critical'}]}]});
let button = elements['#alertBanner'].children[0];
assert.equal(button.tagName, 'button'); assert.equal(button.type, 'button');
button.listeners.click();
let detail = elements['#detailBody'].textContent;
assert(detail.includes('Configured baseline: 2'));
assert(detail.includes('Current / baseline ratio: 3'));
assert(detail.includes('Showing 1 supplied records of 7'));
assert(detail.includes('first 3 records'));
assert(elements['#detail'].classes.has('open'));
const names = ['transatlantic_north', 'transatlantic_south', 'transpacific', 'fourth_corridor', 'fifth_corridor'];
context.latestData = {cable_health: {source: 'nga-msi', _stale: true, corridors: Object.fromEntries(names.map(name => [name, {status_score: 3, status_label: 'disrupted', cables: ['Test cable'], relevant_warnings: [{issueDate: '2026-10-15', text_snippet: '<img src=x onerror=bad>', status: 'ACTIVE', msgNumber: 12, url: 'javascript:bad'}]}]))}};
context.updateAlertBanner({alerts: [{domain: 'infrastructure', priority: 'bad" onclick="bad', corridors: names, value: 5, message: '5 corridors, first three in summary'}]});
button = elements['#alertBanner'].children[0];
assert.equal(button.className, 'alert-strip medium');
button.listeners.click();
detail = elements['#detailBody'].textContent;
for (const name of names) assert(detail.includes(name.replaceAll('_', ' ')));
assert(detail.includes('not confirmed cable damage'));
assert(detail.includes('Unavailable, stale or degraded'));
assert(detail.includes('<img src=x onerror=bad>')); // literal text, never an element
assert(!elements['#detailBody'].children.some(c => ['img', 'script', 'a'].includes(c.tagName)));
context.updateAlertBanner({alerts: [{domain: 'new-domain', message: 'Unknown alert', evidence: '<script>bad</script>'}]});
elements['#alertBanner'].children[0].listeners.click();
assert(elements['#detailBody'].textContent.includes('<script>bad</script>'));
context.updateAlertBanner({_stale: true, alerts: [{domain: 'military'}]});
assert.equal(elements['#alertBanner'].children.length, 0);
assert(elements['#alertBanner'].textContent.includes('cannot be confirmed'));
// Exercise the actual update/cache boundary with an old snapshot. Every retained
// map callback is a spy; accessing a removed group would throw and fail the test.
const updates = [];
const updateAll = source('updateAll');
for (const name of new Set(updateAll.match(/updateMap\w+/g))) context[name] = () => updates.push(name);
context.updateDrawer = () => {};
context.updateTicker = () => {};
context.window = {};
context.DASHBOARD_CACHE_KEY = 'test';
let cached;
context._writeCacheEnvelope = (key, payload) => { cached = payload; };
context.console = {warn: (...args) => { throw new Error(args.join(' ')); }};
vm.runInContext(updateAll, context);
context.updateAll({webcams: {count: 9}, migration: {corridors: []}, signal_convergence: {hotspots: []}, timestamp: '2026-10-16'});
assert(updates.length >= 10);
for (const key of ['webcams', 'migration', 'signal_convergence']) {
    assert(!(key in cached)); assert(!(key in context.latestData));
}
context.localStorage.getItem = () => JSON.stringify({webcams: true, migration: true, convergence: true, quakes: false});
const savedStart = script.indexOf('var layerState =');
const savedEnd = script.indexOf('var drawerOpen =', savedStart);
vm.runInContext(script.slice(savedStart, savedEnd), context);
assert.equal(context.layerState.quakes, false);
for (const key of ['webcams', 'migration', 'convergence']) assert(!(key in context.layerState));
assert.equal(requests.length, 0);
console.log('PASS: syntax, HUD actions, group bounds, retired-cache filtering, alert evidence, no network');

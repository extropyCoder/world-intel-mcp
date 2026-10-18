// Engine-neutral ES5 DOM stub for Node or the installed Duktape library.
// Production function declarations are prepended by the Python test runner.
// This models focus removal and section reconstruction, not browser layout.
var checks = 0;
function check(ok, message) { if (!ok) throw new Error(message); checks++; }
function Element(tag) {
    this.tagName = tag || 'div'; this.children = []; this.dataset = {};
    this.attrs = {}; this.listeners = {}; this.style = {}; this._text = '';
    var classes = {};
    this.classList = {
        add: function(name) { classes[name] = true; },
        remove: function(name) { delete classes[name]; },
        contains: function(name) { return !!classes[name]; }
    };
}
Element.prototype.contains = function(element) {
    return this === element || this.children.some(function(child) { return child.contains(element); });
};
Element.prototype.replaceChildren = function() {
    if (this.contains(document.activeElement)) document.activeElement = document.body;
    this.children = []; this._text = '';
};
Element.prototype.appendChild = function(child) { this.children.push(child); return child; };
Element.prototype.querySelectorAll = function() {
    return this.children.filter(function(child) { return child.tagName === 'button' && child.dataset.focusKey; });
};
Element.prototype.setAttribute = function(name, value) { this.attrs[name] = value; };
Element.prototype.addEventListener = function(name, fn) { this.listeners[name] = fn; };
Element.prototype.focus = function() { document.activeElement = this; };
Element.prototype.scrollIntoView = function() { this.scrolled = true; };
Object.defineProperty(Element.prototype, 'textContent', {
    get: function() { return this._text + this.children.map(function(child) { return child.textContent; }).join(' '); },
    set: function(text) { this.replaceChildren(); this._text = String(text); }
});
Object.defineProperty(Element.prototype, 'innerHTML', {
    get: function() { return this._text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); },
    set: function(html) {
        this.renderedHTML = html;
        this.replaceChildren();
        // Parse just the real renderer's section headings and adjoining content.
        // Preserve order/sibling traversal for its actual collapse-restore loop.
        var pattern = /<div class="sh"(?: data-section="([^"]*)")?>([^<]*)<\/div>/g;
        var match;
        while ((match = pattern.exec(html))) {
            var heading = new Element(); heading.classList.add('sh');
            heading.textContent = match[2]; heading.dataset.section = match[1];
            this.appendChild(heading); this.appendChild(new Element());
        }
        this.children.forEach(function(child, index, children) { child.nextElementSibling = children[index + 1] || null; });
    }
});
var document = {body: new Element('body'), activeElement: null, createElement: function(tag) { return new Element(tag); }};
document.activeElement = document.body;
var elements = {};
['hudStats', 'hudFeedback', 'alertBanner', 'drawer', 'drawerToggle', 'drawerBody'].forEach(function(id) { elements['#' + id] = new Element(); });
function $(selector) { if (!elements[selector]) throw new Error('Unexpected selector ' + selector); return elements[selector]; }
function $$(selector) {
    if (selector !== '#drawerBody .sh') throw new Error('Unexpected selector ' + selector);
    return $('#drawerBody').children.filter(function(child) { return child.classList.contains('sh'); });
}
document.querySelector = function(selector) {
    if (selector === '#drawerBody [data-section="cyber"]') return $$('#drawerBody .sh').filter(function(heading) { return heading.dataset.section === 'cyber'; })[0] || null;
    return elements[selector] || null;
};
var storage = {};
var localStorage = {getItem: function(key) { return storage[key] || null; }, setItem: function(key, value) { storage[key] = value; }};
var fitHeader = function() {}, fmtBigPlain = String;
var safe = function(html) { return html; }; // Renderer receives only these fixed test fixtures.
var esc = String;
var _bannerAlerts = [], _bannerDigest = null, drawerOpen = false;
var _drawerItems = {}, _drawerIdx = 0;
var detailsOpened = [];
function showAlertDetail(index) { detailsOpened.push(_bannerAlerts[index]); }
var outside = new Element('input');
var hud = $('#hudStats'), banner = $('#alertBanner');
function action(target) { return hud.children.filter(function(button) { return button.dataset.hudTarget === target; })[0]; }
function alertButton(key) { return banner.children.filter(function(button) { return button.dataset.focusKey === key; })[0]; }
var firstHud = {military_flights: {count: 2}, cyber_threats: {threats: []}};
updateHudStats(firstHud);
action('military').focus();
updateHudStats({earthquakes: {earthquakes: []}, military_flights: {count: 3}, cyber_threats: {threats: []}});
check(document.activeElement === action('military'), 'HUD focus must survive insertion/reordering and count changes');
updateHudStats({earthquakes: {earthquakes: []}, military_flights: {count: 3}, cyber_threats: {threats: []}, traffic_flow: {count: 1, global_avg_congestion: 4}});
check(document.activeElement === action('military'), 'HUD focus must survive an unrelated source tick');
check(document.activeElement.tagName === 'button' && document.activeElement.type === 'button', 'restored HUD control remains a native button');
outside.focus(); updateHudStats(firstHud);
check(document.activeElement === outside, 'HUD update must not steal outside focus');
action('military').focus(); updateHudStats({});
check(document.activeElement === document.body, 'removed HUD action must not transfer focus');
var military = {domain: 'military', priority: 'high', message: '1 anomaly', surges: [{}], value: 1};
var cable = {domain: 'infrastructure', corridors: ['a'], message: 'Cable warning'};
var internet = {domain: 'infrastructure', message: '5 internet outages active', value: 5};
updateAlertBanner({alerts: [military, cable, internet]});
var militaryKey = alertControlKey(military);
alertButton(militaryKey).focus();
var changedMilitary = {domain: 'military', priority: 'critical', message: '2 anomalies', surges: [{}, {}], value: 2};
updateAlertBanner({alerts: [{id: 'inserted', domain: 'security', message: 'New alert'}, internet, changedMilitary, cable]});
check(document.activeElement === alertButton(militaryKey), 'alert focus must follow rule identity after reorder and evidence/count/priority changes');
document.activeElement.listeners.click();
check(detailsOpened[0] === changedMilitary, 'reordered alert click must open the current evidence, not its old index');
updateAlertBanner({timestamp: 'unrelated-tick', alerts: [internet, changedMilitary, cable]});
check(document.activeElement === alertButton(militaryKey), 'alert focus must survive consecutive unrelated updates');
var cableKey = alertControlKey(cable);
alertButton(cableKey).focus(); updateAlertBanner({alerts: [cable, internet, changedMilitary]});
check(document.activeElement === alertButton(cableKey), 'cable and internet alerts must have distinct identities within the same domain');
outside.focus(); updateAlertBanner({alerts: [military]});
check(document.activeElement === outside, 'alert update must not steal outside focus');
alertButton(militaryKey).focus();
updateAlertBanner({alerts: [cable, internet, {id: 'new', domain: 'security', message: 'New alert'}, military]});
check(document.activeElement === document.body, 'truncation must not focus the alert newly occupying the previous index');
updateAlertBanner({alerts: [military]}); alertButton(militaryKey).focus();
updateAlertBanner({alerts: [military, changedMilitary]});
check(document.activeElement === document.body, 'duplicate/ambiguous alert identities must not transfer focus');
updateAlertBanner({alerts: [military]}); alertButton(militaryKey).focus();
updateAlertBanner({_stale: true, alerts: [military]});
check(document.activeElement === document.body, 'unavailable alerts must not retain phantom focus');
check(alertControlKey({domain: 'custom', id: 'a', message: 'new'}) !== alertControlKey({domain: 'custom', id: 'b', message: 'new'}), 'explicit alert IDs distinguish different alerts');
updateAlertBanner({alerts: [internet]}); banner.children[0].focus();
updateAlertBanner({alerts: [{domain: 'infrastructure', message: 'An unrelated infrastructure alert'}]});
check(document.activeElement === document.body, 'unknown alerts must not inherit a different rule identity merely from their domain');
updateAlertBanner({alerts: [{id: 'stable', domain: 'custom', message: 'Old summary'}]}); banner.children[0].focus();
updateAlertBanner({alerts: [{id: 'stable', domain: 'custom', message: 'Updated summary'}]});
check(document.activeElement === banner.children[0], 'explicit source identity must survive summary changes');
// Restoration also respects a focusout handler moving somewhere else while
// replacement is in progress, rather than pulling focus back from that control.
updateHudStats(firstHud); action('military').focus();
var restore = rememberControlFocus(hud);
hud.replaceChildren(); outside.focus();
var replacement = new Element('button'); replacement.dataset.focusKey = JSON.stringify(['hud', 'map', 'military']);
hud.appendChild(replacement); restore();
check(document.activeElement === outside, 'restoration must not override an intervening outside focus change');

// Start from persisted collapse state, exercise the actual drawer renderer,
// explicit Cyber open, then the same renderer used on the following SSE tick.
localStorage.setItem('phoenix-drawer-collapsed', JSON.stringify(['CYBER / SECURITY', 'MARKETS', 'UNRELATED SECTION']));
var drawerData = {cyber_threats: {threats: []}};
updateDrawer(drawerData);
var cyberHeading = document.querySelector('#drawerBody [data-section="cyber"]');
check(cyberHeading && cyberHeading.classList.contains('collapsed'), 'fixture must really reconstruct a collapsed Cyber section');
activateHudAction('drawer', 'cyber');
check(!cyberHeading.classList.contains('collapsed') && cyberHeading.nextElementSibling.style.display === '', 'Cyber action must expand content');
check(JSON.parse(storage['phoenix-drawer-collapsed']).join('|') === 'MARKETS|UNRELATED SECTION', 'explicit Cyber open must persist removal and preserve all other collapsed sections');
updateDrawer(drawerData);
cyberHeading = document.querySelector('#drawerBody [data-section="cyber"]');
check(!cyberHeading.classList.contains('collapsed') && cyberHeading.nextElementSibling.style.display !== 'none', 'next SSE drawer reconstruction must keep Cyber open');
var markets = $$('#drawerBody .sh').filter(function(heading) { return heading.textContent === 'MARKETS'; })[0];
check(markets && markets.classList.contains('collapsed'), 'next SSE tick must still restore other collapsed sections');
check(drawerOpen, 'Cyber action must synchronize drawer-open state');
checks;

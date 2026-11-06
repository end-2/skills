#!/usr/bin/env node
// Private, fixed JSONL protocol. No caller-supplied JavaScript is evaluated.
import {readFile} from 'node:fs/promises';
import {join} from 'node:path';
import {createHash, randomUUID} from 'node:crypto';
import {createInterface} from 'node:readline';
import {collectDocument} from './dom_snapshot.mjs';

const MAX_NODES = 1200;
const MAX_TEXT = 2048;
const MAX_TABS = 128;
const options = JSON.parse(process.argv[2] || '{}');
const timeout = options.timeout_ms || 40000;
let puppeteer, browser, browserSession, selectedTabId;
let connectionId = randomUUID();
let lastSnapshot = null;
const pages = new Map();
const frameIds = new WeakMap();
const registries = new Map();
const observedNodes = new Map();
const dialogObservers = new Set();
let frameSequence = 0;

function fail(code, message, details = {}) {
  const error = new Error(message);
  Object.assign(error, {code, details});
  throw error;
}

function reply(value) { process.stdout.write(`${JSON.stringify(value)}\n`); }

function compact(text, node, field) {
  text = String(text ?? '');
  if (text.length > MAX_TEXT) {
    node[`${field}_truncated`] = true;
    node[`${field}_sha256`] = createHash('sha256').update(text).digest('hex');
  }
  node[field] = text.slice(0, MAX_TEXT);
}

function frameId(frame) {
  if (!frameIds.has(frame)) frameIds.set(frame, `frame-${++frameSequence}`);
  return frameIds.get(frame);
}

async function disconnect() {
  lastSnapshot = null;
  observedNodes.clear();
  registries.clear();
  pages.clear();
  browserSession = null;
  const existing = browser;
  browser = null;
  if (existing) await existing.disconnect();
}

async function ensureBrowser() {
  if (browser?.connected) return;
  await disconnect();
  connectionId = randomUUID();
  selectedTabId = null;
  const config = {
    defaultViewport: null,
    protocolTimeout: timeout,
    wsOptions: {handshakeTimeout: timeout, keepAlive: true, maxPayload: 32 * 1024 * 1024},
    // These passive observers are unnecessary for structured DOM control.
    networkEnabled: false,
    issuesEnabled: false,
  };
  if (options.browser_url) {
    const url = new URL(options.browser_url);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port ||
        url.username || url.password || !['', '/'].includes(url.pathname) || url.search || url.hash) {
      fail('invalid_configuration', 'browser_url must be http://127.0.0.1:PORT');
    }
    config.browserURL = url.href;
  } else if (options.user_data_dir) {
    let content;
    try { content = await readFile(join(options.user_data_dir, 'DevToolsActivePort'), 'utf8'); }
    catch { fail('connection_required', 'Enable remote debugging at chrome://inspect/#remote-debugging in the running Chrome; DevToolsActivePort is unavailable'); }
    const lines = content.trim().split(/\r?\n/);
    if (lines.length !== 2 || !/^\d+$/.test(lines[0]) || Number(lines[0]) < 1 || Number(lines[0]) > 65535 ||
        !/^\/devtools\/browser(?:\/[A-Za-z0-9-]+)?$/.test(lines[1])) {
      fail('invalid_configuration', 'Invalid DevToolsActivePort');
    }
    config.browserWSEndpoint = `ws://127.0.0.1:${Number(lines[0])}${lines[1]}`;
  } else {
    config.channel = 'chrome';
  }
  try {
    browser = await puppeteer.connect(config);
    browserSession = await browser.target().createCDPSession();
    browser.on('disconnected', () => { lastSnapshot = null; });
  } catch {
    await disconnect();
    fail('connection_required', 'Could not attach to existing Chrome. Enable remote debugging at chrome://inspect/#remote-debugging and allow Chrome’s connection prompt. No browser was launched.');
  }
}

async function refreshPages() {
  const targets = browser.targets().filter(target => target.type() === 'page');
  const activeIds = new Set();
  for (const target of targets.slice(0, MAX_TABS)) {
    let record = [...pages.values()].find(item => item.target === target);
    if (!record) {
      const page = await target.page();
      if (!page) continue;
      const cdp = await target.createCDPSession();
      const {targetInfo} = await cdp.send('Target.getTargetInfo');
      record = {id: targetInfo.targetId, target, page, cdp, dialog: null};
      pages.set(record.id, record);
      page.setDefaultTimeout(Math.min(timeout, 10000));
      page.setDefaultNavigationTimeout(Math.min(timeout, 15000));
      page.on('dialog', dialog => {
        record.dialog = dialog;
        for (const observer of dialogObservers) observer(record);
      });
      page.on('framenavigated', frame => {
        const old = registries.get(frame);
        registries.delete(frame);
        old?.handle.dispose().catch(() => {});
      });
      page.on('framedetached', frame => {
        const old = registries.get(frame);
        registries.delete(frame);
        old?.handle.dispose().catch(() => {});
      });
      page.on('close', () => {
        pages.delete(record.id);
        for (const [frame, registry] of registries) {
          if (frame.page() === page) {
            registries.delete(frame);
            registry.handle.dispose().catch(() => {});
          }
        }
      });
    }
    activeIds.add(record.id);
  }
  for (const [id, record] of pages) {
    if (!activeIds.has(id)) {
      pages.delete(id);
      await record.cdp.detach().catch(() => {});
    }
  }
  const {targetInfos} = await browserSession.send('Target.getTargets');
  const infos = new Map(targetInfos.map(info => [info.targetId, info]));
  const tabs = [...pages.values()].map(record => {
    const info = infos.get(record.id);
    const tab = {id: record.id, url: info?.url ?? record.page.url()};
    compact(info?.title || '', tab, 'title');
    return tab;
  }).sort((a, b) => a.id.localeCompare(b.id));
  if (!selectedTabId || !pages.has(selectedTabId)) selectedTabId = tabs[0]?.id ?? null;
  return {tabs, truncated: targets.length > MAX_TABS};
}

async function registryFor(frame) {
  let registry = registries.get(frame);
  if (registry) {
    try {
      if (await registry.handle.evaluate(state => state.document === document)) return registry;
    } catch { /* A navigation destroys the old execution context. */ }
    await registry.handle.dispose().catch(() => {});
  }
  const nonce = randomUUID();
  // isolatedRealm is an internal Puppeteer API deliberately pinned to 25.16.0.
  // Keeping this registry out of the page's JS realm prevents page code from
  // substituting an element behind an observed ID. Upgrade only with smoke tests.
  const handle = await frame.isolatedRealm().evaluateHandle(() => ({
    document, ids: new WeakMap(), elements: new Map(), nextId: 1,
  }));
  registry = {handle, nonce, frame};
  registries.set(frame, registry);
  return registry;
}


async function snapshot() {
  await ensureBrowser();
  const listed = await refreshPages();
  observedNodes.clear();
  const state = {
    platform: 'chrome', browser_id: connectionId, tabs: listed.tabs,
    selected_tab_id: selectedTabId, roots: [], nodes: [], frames: [],
    truncated: listed.truncated,
    capabilities: {scope: 'selected_tab_dom', screenshots: false, accessibility: false,
      dom: true, native_chrome_ui: false, cross_origin_frames: true,
      closed_shadow_roots: false, select_tab: true, new_tab: true,
      navigate: true, close_tab: true, press_key: true, scroll: true, handle_dialog: true},
  };
  const record = pages.get(selectedTabId);
  if (!record) { lastSnapshot = state; return state; }
  const tabRoot = `tab:${record.id}`;
  state.roots.push(tabRoot);
  state.nodes.push({id: tabRoot, tab_id: record.id, role: 'tab', title: listed.tabs.find(tab => tab.id === record.id)?.title || '',
    children: [], actions: [], settable: []});
  if (record.dialog) {
    state.pending_dialog = {tab_id: record.id, type: record.dialog.type()};
    compact(record.dialog.message(), state.pending_dialog, 'message');
    state.truncated = true;
    state.observation_note = 'Page JavaScript is paused by the observed dialog; handle it before inspecting DOM.';
    lastSnapshot = state;
    return state;
  }
  const frames = record.page.frames();
  const frameRoots = new Map(frames.map(frame => [frame, `frame:${record.id}:${frameId(frame)}`]));
  for (const frame of frames) {
    if (state.nodes.length >= MAX_NODES) { state.truncated = true; break; }
    const fid = frameId(frame);
    const frameRoot = frameRoots.get(frame);
    const parent = state.nodes.find(node => node.id === frameRoots.get(frame.parentFrame())) || state.nodes[0];
    parent.children.push(frameRoot);
    const root = {id: frameRoot, tab_id: record.id, frame_id: fid, role: 'document', title: frame.url(), children: [], actions: [], settable: []};
    state.nodes.push(root);
    const frameInfo = {id: fid, url: frame.url(), parent_id: frame.parentFrame() ? frameId(frame.parentFrame()) : null};
    state.frames.push(frameInfo);
    try {
      const registry = await registryFor(frame);
      frameInfo.document_id = registry.nonce;
      const result = await registry.handle.evaluate(collectDocument, Math.max(0, MAX_NODES - state.nodes.length));
      frameInfo.scroll = result.scroll;
      frameInfo.viewport = result.viewport;
      frameInfo.ready_state = result.ready_state;
      frameInfo.has_focus = result.has_focus;
      compact(result.title, root, 'title');
      state.truncated ||= result.truncated;
      const prefix = `${record.id}:${fid}:${registry.nonce}:`;
      const converted = new Map();
      for (const item of result.nodes) {
        const {local_id, parent_id, ...node} = item;
        node.id = prefix + local_id;
        node.tab_id = record.id;
        node.frame_id = fid;
        compact(node.title, node, 'title');
        if ('value' in node) compact(node.value, node, 'value');
        if (node.title_truncated || node.value_truncated) state.truncated = true;
        converted.set(local_id, {node, parent_id});
        observedNodes.set(node.id, {registry, localId: local_id, tabId: record.id, node});
        state.nodes.push(node);
      }
      for (const {node, parent_id} of converted.values()) {
        (converted.get(parent_id)?.node || root).children.push(node.id);
      }
    } catch {
      state.truncated = true;
      frameInfo.unavailable = true;
      root.description = 'Frame DOM unavailable or changed while being observed; inspect again before acting.';
    }
  }
  lastSnapshot = state;
  return state;
}

function selectedRecord(action) {
  const record = pages.get(action.tab_id);
  if (!record || action.tab_id !== selectedTabId) fail('element_unavailable', 'Select the observed tab and read status before this action');
  return record;
}

function validURL(value) {
  try {
    const parsed = new URL(value);
    if ((['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password) || value === 'about:blank') return value;
  } catch { /* Rejected below. */ }
  fail('invalid_action', 'URL must be http, https, or about:blank without embedded credentials');
}

async function act(action) {
  if (!browser?.connected || !lastSnapshot) fail('element_unavailable', 'Read status from a live browser before acting');
  lastSnapshot = null; // Every dispatch consumes the observation, even on failure.
  const kind = action.kind;
  if (kind === 'select_tab') {
    if (!pages.has(action.tab_id)) fail('element_unavailable', 'Tab was not observed');
    selectedTabId = action.tab_id; // Logical selection; no foreground/focus change.
    return {performed: true, selected_tab_id: selectedTabId};
  }
  if (kind === 'new_tab') {
    validURL(action.url);
    const page = await browser.newPage();
    await refreshPages();
    const record = [...pages.values()].find(item => item.page === page);
    if (record) selectedTabId = record.id;
    if (action.url !== 'about:blank') await page.goto(action.url, {waitUntil: 'domcontentloaded', timeout: Math.min(timeout, 15000)});
    return {performed: true, selected_tab_id: selectedTabId};
  }
  const record = kind === 'close_tab' ? pages.get(action.tab_id) : selectedRecord(action);
  if (!record) fail('element_unavailable', 'Tab was not observed');
  if (kind === 'handle_dialog') {
    const dialog = record.dialog;
    const pending = record.pendingAction;
    if (!dialog) fail('element_unavailable', 'No dialog was observed');
    if (action.response === 'accept') await dialog.accept(action.text);
    else if (action.response === 'dismiss') await dialog.dismiss();
    else fail('invalid_action', 'Unknown dialog response');
    if (record.dialog === dialog) record.dialog = null;
    if (record.pendingAction === pending) record.pendingAction = null;
    if (pending) await pending;
  } else if (record.dialog) {
    fail('dialog_open', 'Handle the observed dialog before other page actions');
  } else if (kind === 'navigate') {
    await record.page.goto(validURL(action.url), {waitUntil: 'domcontentloaded', timeout: Math.min(timeout, 15000)});
  } else if (kind === 'close_tab') {
    await record.page.close({runBeforeUnload: false});
    if (selectedTabId === action.tab_id) selectedTabId = null;
  } else if (kind === 'scroll') {
    const sign = ['up', 'left'].includes(action.direction) ? -1 : 1;
    const horizontal = ['left', 'right'].includes(action.direction);
    if (!['up', 'down', 'left', 'right'].includes(action.direction) || !Number.isFinite(action.pixels) || action.pixels < 1 || action.pixels > 5000) fail('invalid_action', 'Invalid scroll');
    // A fixed page scroll operation is independent of the current mouse target.
    await record.page.mainFrame().isolatedRealm().evaluate((x, y) => scrollBy({left: x, top: y, behavior: 'instant'}), horizontal ? sign * action.pixels : 0, horizontal ? 0 : sign * action.pixels);
  } else if (kind === 'press_key') {
    const pieces = action.key.split('+');
    const key = pieces.pop();
    const allowed = new Set(['Alt', 'Control', 'Meta', 'Shift']);
    if (!key || pieces.some(modifier => !allowed.has(modifier)) || new Set(pieces).size !== pieces.length) fail('invalid_action', 'Invalid key chord');
    const held = [];
    try {
      for (const modifier of pieces) { held.push(modifier); await record.page.keyboard.down(modifier); }
      await record.page.keyboard.press(key);
    } finally {
      for (const modifier of held.reverse()) await record.page.keyboard.up(modifier);
    }
  } else if (['click', 'hover', 'focus', 'fill'].includes(kind)) {
    const observed = observedNodes.get(action.node_id);
    if (!observed || observed.tabId !== action.tab_id) fail('element_unavailable', 'Node was not observed in this tab');
    const {registry, localId, node} = observed;
    const capability = kind === 'fill' ? node.settable.includes('value') : kind === 'focus' ? node.settable.includes('focused') : node.actions.includes(kind);
    if (!capability || node.enabled === false || (kind === 'fill' && node.value_truncated)) fail('unsupported', 'Node does not support this action');
    const handle = await registry.handle.evaluateHandle((state, id) => {
      if (state.document !== document) return null;
      const element = state.elements.get(id);
      return element?.isConnected && element.ownerDocument === document ? element : null;
    }, localId);
    try {
      const element = handle.asElement();
      if (!element) fail('element_unavailable', 'Observed element is no longer connected');
      if (kind === 'click') await element.click();
      else if (kind === 'hover') await element.hover();
      else if (kind === 'focus') await element.focus();
      else {
        // Locator conditions run in the frame's main realm. Adopt the exact
        // existing DOM handle; never resolve a selector that could retarget.
        const fillHandle = await registry.frame.mainRealm().adoptHandle(element);
        try { await fillHandle.asLocator().setTimeout(Math.min(timeout, 10000)).fill(action.value); }
        finally { await fillHandle.dispose(); }
      }
    } finally { await handle.dispose(); }
  } else {
    fail('unsupported', 'Unknown browser action');
  }
  return {performed: true, kind};
}

async function dispatchAction(action) {
  let signalDialog;
  const dialogOpened = new Promise(resolve => { signalDialog = resolve; });
  const observer = record => {
    if (record.id === action.tab_id || (action.kind === 'new_tab' && record.id === selectedTabId)) signalDialog(record);
  };
  dialogObservers.add(observer);
  const pending = act(action);
  try {
    const outcome = await Promise.race([
      pending.then(result => ({result})),
      dialogOpened.then(record => ({record})),
    ]);
    if (outcome.record) {
      // CDP input may not acknowledge until the modal is handled. Keep this
      // exact operation alive, without retrying or autoaccepting the dialog.
      // handle_dialog waits for its completion/cleanup before another action.
      outcome.record.pendingAction = pending;
      pending.catch(() => {});
      return {performed: false, completion_pending: true, dialog_open: true, kind: action.kind};
    }
    return outcome.result;
  } finally { dialogObservers.delete(observer); }
}

try {
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (major < 22 || (major === 22 && minor < 12)) fail('dependency_missing', 'Node.js >=22.12.0 is required');
  try { puppeteer = (await import('puppeteer-core')).default; }
  catch { fail('dependency_missing', 'Install the pinned browser dependency: run npm ci in chrome-use-server/scripts'); }
  reply({ok: true, result: {ready: true, node_version: process.versions.node}});
  const lines = createInterface({input: process.stdin, crlfDelay: Infinity});
  for await (const line of lines) {
    let operation;
    try {
      if (line.length > 131072) fail('invalid_request', 'Request exceeds 128 KiB');
      const payload = JSON.parse(line);
      operation = payload.op;
      if (operation === 'close') { await disconnect(); break; }
      if (!['snapshot', 'act'].includes(operation)) fail('invalid_request', 'Unknown worker operation');
      const result = operation === 'snapshot' ? await snapshot() : await dispatchAction(payload.action);
      reply({ok: true, result});
    } catch (error) {
      reply({ok: false, error: {code: error.code || 'browser_error',
        message: error.code ? error.message : 'Chrome operation failed; inspect status before continuing',
        details: {...error.details, requires_snapshot: true, action_outcome_unknown: operation === 'act'}}});
    }
  }
  await disconnect();
} catch (error) {
  reply({ok: false, error: {code: error.code || 'backend_unavailable', message: error.message}});
  process.exitCode = 1;
}

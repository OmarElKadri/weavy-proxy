// weavy-proxy.mjs — OpenAI-compatible image endpoint backed by the user's logged-in Weavy browser.
// /v1/images/generations, /v1/images/edits, /v1/models. All Weavy API calls run inside the real
// browser page via CDP so Cloudflare sees a genuine browser.
//
// Fully self-contained: for every model the proxy creates its OWN flow (POST /recipes/create {}),
// builds a custommodelV2 node from the model's public definition (node-definitions/public), and
// executes it inline (POST /batches/recipes/:id/execute — nodes are carried in the body, matching
// the app's captured traffic). No dependency on any pre-existing user flow.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CFG = JSON.parse(fs.readFileSync(path.join(__dirname, 'config.json'), 'utf8'));
const API = 'https://api.weavy.ai';
const PORT = CFG.port || 8787;
const CDP_URL = CFG.cdpUrl || 'http://127.0.0.1:9222';
const HOME_URL = 'https://app.weavy.ai/flows';
const PROXY_KEY = process.env.WEAVY_PROXY_KEY || '';
const POLL_MS = 2000;
const RUN_TIMEOUT_MS = 6 * 60 * 1000;
const DEFS_CACHE = path.join(__dirname, 'model-definitions.json');
const FLOWS_CACHE = path.join(__dirname, 'proxy-flows.json');

let browser = null;
let page = null;
let firebaseToken = null;
let definitions = null;
let flowsCache = fs.existsSync(FLOWS_CACHE) ? JSON.parse(fs.readFileSync(FLOWS_CACHE, 'utf8')) : {};
let primaryFlowId = flowsCache.primaryFlowId || null;

const log = (...a) => console.log(new Date().toISOString(), ...a);
const saveFlowsCache = () => fs.writeFileSync(FLOWS_CACHE, JSON.stringify(flowsCache, null, 2));
const uuid = () => crypto.randomUUID();
const clone = v => JSON.parse(JSON.stringify(v));

async function connectBrowser() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      browser = await chromium.connectOverCDP(CDP_URL);
      const ctx = browser.contexts()[0] || (await browser.newContext());
      page = ctx.pages().find(p => /app\.weavy\.ai/.test(p.url())) || null;
      if (!page) {
        page = await ctx.newPage();
        await page.goto(HOME_URL, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(e => log('goto warn:', e.message));
      }
      log('CDP connected; page:', page.url());
      return;
    } catch (e) {
      if (Date.now() + 2000 >= deadline) throw new Error(`Cannot reach browser CDP at ${CDP_URL}: ${e.message}. Run start-browser.ps1 first.`);
      await new Promise(r => setTimeout(r, 2000));
    }
  }
}

async function ensurePage() {
  if (!browser || !browser.isConnected()) await connectBrowser();
  if (!page || page.isClosed()) {
    const ctx = browser.contexts()[0] || (await browser.newContext());
    page = await ctx.newPage();
    await page.goto(HOME_URL, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(e => log('goto warn:', e.message));
  }
  if (!/app\.weavy\.ai/.test(page.url())) {
    await page.goto(HOME_URL, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(e => log('goto warn:', e.message));
  }
  return page;
}

async function pageFetch(pathname, { method = 'GET', headers = {}, body, timeoutMs = 60000 } = {}) {
  const p = await ensurePage();
  return p.evaluate(async ({ url, method, headers, body, timeoutMs }) => {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(url, { method, headers, body, credentials: 'include', signal: ctrl.signal });
      return { status: r.status, text: await r.text() };
    } finally { clearTimeout(t); }
  }, { url: API + pathname, method, headers, body, timeoutMs });
}

const parseJson = t => { try { return JSON.parse(t); } catch { return null; } };

async function getFirebaseToken() {
  const p = await ensurePage();
  try {
    return await p.evaluate(async () => {
      const openDb = name => new Promise((res, rej) => { const r = indexedDB.open(name); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
      const db = await openDb('firebaseLocalStorageDb');
      const storeNames = [...db.objectStoreNames];
      if (!storeNames.includes('firebaseLocalStorage')) return null;
      const all = await new Promise((res, rej) => {
        const rq = db.transaction('firebaseLocalStorage', 'readonly').objectStore('firebaseLocalStorage').getAll();
        rq.onsuccess = () => res(rq.result); rq.onerror = () => rej(rq.error);
      });
      for (const row of all || []) {
        const v = row && row.value;
        if (v && v.stsTokenManager && v.stsTokenManager.accessToken) return v.stsTokenManager.accessToken;
      }
      return null;
    });
  } catch { return null; }
}

async function api(pathname, { method = 'GET', json, rawBody, extraHeaders = {}, timeoutMs = 60000, recipeId, _retried = false } = {}) {
  const headers = {
    accept: 'application/json, text/plain, */*',
    'x-app-version': '4.1.1678',
    'x-weavy-auth-provider': 'firebase',
    'x-wv-tier-cookie': '1',
    ...extraHeaders,
  };
  if (recipeId) headers['x-app-recipeid'] = recipeId;
  let body = rawBody;
  if (json !== undefined) { headers['content-type'] = 'application/json'; body = JSON.stringify(json); }
  if (!headers.authorization && firebaseToken) headers.authorization = `Bearer ${firebaseToken}`;
  const res = await pageFetch(pathname, { method, headers, body, timeoutMs });
  const looksHtml = /^\s*<(!doctype|html)/i.test(res.text || '');
  if ((res.status === 401 || res.status === 403) && !looksHtml && !_retried) {
    const tok = await getFirebaseToken();
    if (tok && tok !== firebaseToken) {
      firebaseToken = tok;
      log('obtained Firebase token, retrying', method, pathname);
      return api(pathname, { method, json, rawBody, extraHeaders, timeoutMs, recipeId, _retried: true });
    }
    if (!tok) throw Object.assign(new Error('Not signed in to Weavy in the proxy browser. Sign in at app.weavy.ai in that window.'), { status: 401 });
  }
  if (res.status === 401 || res.status === 403) {
    const j = parseJson(res.text);
    throw Object.assign(new Error((j && (j.message || j.error)) || `Weavy API ${res.status} for ${pathname}`), { status: res.status });
  }
  if (res.status >= 400) {
    const j = parseJson(res.text);
    throw Object.assign(new Error((j && (j.message || j.error || JSON.stringify(j).slice(0, 300))) || `Weavy API ${res.status} for ${pathname}: ${(res.text || '').slice(0, 100).replace(/\s+/g, ' ')}`), { status: res.status });
  }
  return parseJson(res.text) ?? res.text;
}

// ---------- model catalog ----------
async function fetchDefinitions() {
  if (definitions) return definitions;
  if (fs.existsSync(DEFS_CACHE)) {
    try { definitions = JSON.parse(fs.readFileSync(DEFS_CACHE, 'utf8')); } catch { definitions = null; }
  }
  if (!definitions || !definitions.public || !definitions.fetchedAt || Date.now() - Date.parse(definitions.fetchedAt) > 24 * 3600e3) {
    const menu = await api('/api/v1/node-definitions/menu?includeMigrated=true');
    const pub = await api('/api/v1/node-definitions/public');
    definitions = { menu, public: pub, fetchedAt: new Date().toISOString() };
    fs.writeFileSync(DEFS_CACHE, JSON.stringify(definitions));
  }
  return definitions;
}

// Full model definitions look like: {id, type:'custommodelV2', isModel:true, data:{name, model:{name,service,version}, handles, params, schema}, metadata}
function collectModelDefs() {
  const out = [];
  const visit = v => {
    if (Array.isArray(v)) v.forEach(visit);
    else if (v && typeof v === 'object') {
      if (v.data && v.data.model && v.data.model.name && v.type === 'custommodelV2') out.push(v);
      for (const k of Object.keys(v)) visit(v[k]);
    }
  };
  visit(definitions);
  return out;
}

const norm = s => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
const squash = s => norm(s).replace(/\s+/g, '');

function findModelDef(query) {
  const q = norm(query);
  const qs = squash(query);
  const defs = collectModelDefs();
  const scored = defs.map(d => {
    const dN = d.data;
    const names = [dN.name, dN.menu && dN.menu.displayName, dN.model.name, dN.model.displayName];
    const hay = norm(names.join(' '));
    const hayS = squash(names.join(' '));
    let score = 0;
    if (dN.name && norm(dN.name) === q) score += 100;                    // exact display name
    if (squash(dN.name || '') === qs) score += 100;
    if (squash(dN.model.name || '') === qs) score += 90;                 // exact fal id
    if (hayS.includes(qs) && qs.length >= 4) score += 40;                // substring
    for (const tok of q.split(' ')) if (tok && hay.includes(tok)) score += tok.length;
    return { d, score };
  }).filter(x => x.score > 0).sort((a, b) => b.score - a.score);
  return scored.length ? scored[0].d : null;
}

// ---------- node building (mirrors the app's captured execute body) ----------
function buildKind(def, params) {
  const dN = def.data;
  const inputs = Object.entries(dN.handles && dN.handles.input || {})
    .sort((a, b) => (a[1].order || 0) - (b[1].order || 0))
    .map(([, h]) => [
      { id: h.id || h.label, title: h.label, description: h.description || '', validTypes: [h.type], required: !!h.required },
      null, // unconnected: values come from params
    ]);
  const jsType = v => typeof v === 'boolean' ? 'boolean' : typeof v === 'number' ? 'number' : 'string';
  const parameters = [];
  for (const [k, sch] of Object.entries(dN.schema || {})) {
    const pv = params[k];
    if (pv === undefined || pv === null || typeof pv === 'object') continue; // skip seed{seed,isRandom} etc.
    const constraint = sch.type === 'enum' ? { type: 'enum', options: sch.options } : { type: sch.type };
    parameters.push([
      { id: k, title: sch.title || k, description: sch.description || '', constraint, ...(sch.default !== undefined ? { defaultValue: { type: jsType(sch.default), value: sch.default } } : {}) },
      { type: 'value', data: { type: jsType(pv), value: pv } },
    ]);
  }
  return {
    type: 'wildcard',
    model: { type: 'user_defined', name: dN.model.name, version: dN.model.version || dN.model.name, service: dN.model.service, description: dN.description || 'Weavy model' },
    inputs,
    parameters,
  };
}

function buildModelNode(def, { prompt, imageUrl, aspect, resolution } = {}) {
  const dN = clone(def.data);
  const params = { ...dN.params };
  // resolve enums case-insensitively against the model's own schema options
  const fitEnum = (key, want) => {
    const sch = dN.schema && dN.schema[key];
    if (!sch || sch.type !== 'enum' || !Array.isArray(sch.options)) return want;
    const hit = sch.options.find(o => norm(o) === norm(want));
    return hit || null; // null -> keep default
  };
  if (prompt !== undefined && 'prompt' in (dN.handles && dN.handles.input || {})) params.prompt = String(prompt);
  if (imageUrl && 'image_1' in (dN.handles && dN.handles.input || {})) params.image_1 = imageUrl;
  else if (imageUrl && 'image_urls' in params) params.image_urls = imageUrl;
  if (aspect) { const v = fitEnum('aspect_ratio', aspect); if (v) params.aspect_ratio = v; }
  if (resolution) { const v = fitEnum('resolution', resolution); if (v) params.resolution = v; }
  dN.params = params;
  dN.kind = buildKind(def, params);

  return {
    id: uuid(),
    dragHandle: '.node-header',
    owner: null,
    type: def.type || 'custommodelV2',
    visibility: 'private',
    isModel: true,
    position: { x: 170, y: -440 },
    data: dN,
    generations: [], selectedIndex: 0, cameraLocked: false, renderWidth: 1024, renderHeight: 1024,
    result: [], output: {}, selectedOutput: 0,
    input: { prompt: params.prompt, ...(imageUrl ? { images: [imageUrl] } : {}) },
    locked: false, selected: false, width: 460, height: 550, zIndex: 2,
  };
}

// ---------- proxy-owned flow ----------
async function ensurePrimaryFlow() {
  if (primaryFlowId) return primaryFlowId;
  log('creating proxy-owned flow (recipes/create)...');
  const created = await api('/api/v1/recipes/create', { method: 'POST', json: {} });
  const id = created && (created.id || created.recipeId || (created.recipe && created.recipe.id));
  if (!id) throw new Error('recipes/create returned no id: ' + JSON.stringify(created).slice(0, 200));
  primaryFlowId = id;
  flowsCache.primaryFlowId = id;
  saveFlowsCache();
  log('proxy flow created:', id);
  return id;
}

// ---------- run plumbing ----------
function extractMediaUrls(obj, exclude = []) {
  const found = new Set();
  const scan = v => {
    if (typeof v === 'string') {
      for (const m of v.matchAll(/https:\/\/(?:media\.weavy\.ai|res\.cloudinary\.com|d[0-9a-z]+\.cloudfront\.net)\/[^"\\\s]+/g)) found.add(m[0]);
      if (/^https:\/\/[^ ]+$/.test(v.trim()) && /image|media|\.png|\.jpe?g|\.webp/i.test(v)) found.add(v.trim());
    } else if (Array.isArray(v)) v.forEach(scan);
    else if (v && typeof v === 'object') Object.values(v).forEach(scan);
  };
  scan(obj);
  return [...found].filter(u => !exclude.some(x => u === x || u.split('?')[0] === x.split('?')[0]));
}

async function runAndWait(recipeId, node, numberOfRuns, excludeUrls = []) {
  const body = { numberOfRuns, nodes: [node], edges: [] };
  const started = await api(`/api/v1/batches/recipes/${recipeId}/execute`, { method: 'POST', json: body, recipeId });
  const batchId = started.batchId;
  if (!batchId) throw new Error('No batchId in execute response: ' + JSON.stringify(started).slice(0, 300));
  log('batch started:', batchId, 'runs:', (started.recipeRuns || []).length);
  const deadline = Date.now() + RUN_TIMEOUT_MS;
  let lastProgress = '';
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, POLL_MS));
    let st;
    try {
      st = await api(`/api/v1/batches/recipes/${recipeId}/batches/${batchId}/status`, { recipeId });
    } catch (e) {
      log('status poll error (will retry):', e.message.slice(0, 140));
      continue;
    }
    const runs = st.recipeRuns || [];
    const failedRun = runs.find(r => r.error) || null;
    const nodeRuns = runs.flatMap(r => r.nodeRuns || []);
    const states = nodeRuns.map(nr => (nr.status || '').toUpperCase());
    const progress = nodeRuns.map(nr => `${nr.status}${nr.progress != null ? ' ' + nr.progress + '%' : ''}`).join(', ');
    if (progress && progress !== lastProgress) { lastProgress = progress; log('progress:', progress); }
    if (failedRun || states.some(s => /FAIL|ERROR|CANCEL/.test(s))) {
      const errText = failedRun && failedRun.error || JSON.stringify(nodeRuns.filter(nr => /FAIL|ERROR|CANCEL/.test((nr.status || '').toUpperCase()))).slice(0, 400);
      throw new Error('Weavy run failed: ' + errText);
    }
    if (states.length && states.every(s => /COMPLETED|SUCCEED/.test(s))) {
      const urls = extractResults(nodeRuns, numberOfRuns, excludeUrls);
      log('run completed; urls:', urls.length);
      if (!urls.length) throw new Error('Run completed but no media URL found in: ' + JSON.stringify(nodeRuns).slice(0, 400));
      return { urls, credits: st.remainingCredits ?? st.userRemainingCredits ?? null };
    }
  }
  throw new Error('Timed out waiting for Weavy run');
}

// nodeRun.result is an array of {url, type, width, height, viewUrl, thumbnailUrl, ...}
function extractResults(nodeRuns, limit, excludeUrls = []) {
  const out = [];
  const excluded = u => excludeUrls.some(x => u === x || u.split('?')[0] === x.split('?')[0]);
  for (const nr of nodeRuns) {
    const res = nr && nr.result;
    const arr = Array.isArray(res) ? res : res != null ? [res] : [];
    for (const r of arr) {
      const u = typeof r === 'string' ? r : r && (r.url || r.originalUrl || r.viewUrl);
      if (u && /^https:\/\//.test(u) && !excluded(u)) out.push(u);
    }
  }
  if (!out.length) return extractMediaUrls({ nodeRuns: nodeRuns.map(nr => nr.result) }, excludeUrls).slice(0, limit);
  return [...new Set(out)].slice(0, limit);
}

async function runModel(def, { prompt, imageUrl, aspect, resolution, n }) {
  const flowId = await ensurePrimaryFlow();
  const node = buildModelNode(def, { prompt, imageUrl, aspect, resolution });
  // the server rejects execute for nodes that are not persisted in the recipe first (observed:
  // "Unable to run this workflow" without a prior save; save 201 -> execute 201 -> run)
  try {
    await api(`/api/v1/recipes/${flowId}/save`, { method: 'POST', json: { name: 'proxy', nodes: [node], edges: [] }, recipeId: flowId });
  } catch (e) {
    log('save warning:', e.message.slice(0, 140));
  }
  return runAndWait(flowId, node, Math.min(Math.max(n || 1, 1), 4), imageUrl ? [imageUrl] : []);
}

// ---------- OpenAI surface ----------
function mapSize(size, def) {
  if (!size || size === 'auto') return {};
  const m = /^(\d+)x(\d+)$/.exec(String(size));
  if (!m) return {};
  const w = +m[1], h = +m[2];
  const ratio = w / h;
  const opts = (((def.data || {}).schema || {}).aspect_ratio || {}).options || [];
  const table = [[1, '1:1'], [4 / 3, '4:3'], [3 / 2, '3:2'], [16 / 9, '16:9'], [21 / 9, '21:9'], [9 / 16, '9:16'], [2 / 3, '2:3'], [3 / 4, '3:4'], [4 / 5, '4:5'], [5 / 4, '5:4']];
  let best = null;
  for (const [r, name] of table) {
    if (opts.some(o => norm(o) === norm(name)) && (!best || Math.abs(ratio - r) < Math.abs(ratio - best[0]))) best = [r, name];
  }
  const aspect = best ? best[1] : undefined;
  const rOpts = (((def.data || {}).schema || {}).resolution || {}).options || [];
  let resolution;
  const px = Math.max(w, h);
  for (const [max, name] of [[600, '512'], [1600, '1K'], [2300, '2K'], [99999, '4K']]) {
    const hit = rOpts.find(o => norm(o) === norm(name));
    if (hit && px <= max) { resolution = hit; break; }
    if (hit) resolution = hit;
  }
  return { aspect, resolution };
}

function parseMultipart(buf, contentType) {
  const m = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(contentType || '');
  if (!m) return null;
  const boundary = '--' + (m[1] || m[2]).trim();
  const parts = [];
  let start = buf.indexOf(boundary);
  while (start !== -1) {
    const next = buf.indexOf(boundary, start + boundary.length);
    if (next === -1) break;
    let chunk = buf.slice(start + boundary.length, next);
    if (chunk[0] === 13 && chunk[1] === 10) chunk = chunk.slice(2);
    if (chunk[chunk.length - 2] === 13 && chunk[chunk.length - 1] === 10) chunk = chunk.slice(0, -2);
    const headerEnd = chunk.indexOf('\r\n\r\n');
    if (headerEnd !== -1) {
      const head = chunk.slice(0, headerEnd).toString('utf8');
      const nameM = /name="([^"]*)"/i.exec(head);
      const fileM = /filename="([^"]*)"/i.exec(head);
      const typeM = /content-type:\s*([^\r\n]+)/i.exec(head);
      parts.push({ name: nameM ? nameM[1] : '', filename: fileM ? fileM[1] : undefined, contentType: typeM ? typeM[1].trim() : undefined, data: chunk.slice(headerEnd + 4) });
    }
    start = next;
  }
  return parts;
}

async function uploadImageInPage(buf, filename, contentType) {
  const p = await ensurePage();
  const b64 = buf.toString('base64');
  const res = await p.evaluate(async ({ b64, filename, contentType, auth }) => {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const fd = new FormData();
    fd.append('file', new Blob([bytes], { type: contentType || 'image/png' }), filename || 'image.png');
    const headers = { 'x-app-version': '4.1.1678', 'x-weavy-auth-provider': 'firebase', 'x-wv-tier-cookie': '1', accept: 'application/json, text/plain, */*' };
    if (auth) headers.authorization = auth;
    const r = await fetch('https://api.weavy.ai/api/v1/assets/upload-asset?runType=workflowExecution', {
      method: 'POST', body: fd, credentials: 'include', headers,
    });
    return { status: r.status, text: await r.text() };
  }, { b64, filename, contentType, auth: firebaseToken ? `Bearer ${firebaseToken}` : null });
  if (res.status === 401 || res.status === 403) {
    // refresh token once and retry
    const tok = await getFirebaseToken();
    if (tok) { firebaseToken = tok; return uploadImageInPage(buf, filename, contentType); }
  }
  if (res.status >= 400) throw Object.assign(new Error('upload-asset ' + res.status + ': ' + res.text.slice(0, 300)), { status: res.status });
  const j = parseJson(res.text) || {};
  const asset = j.asset || j;
  const url = (asset && (asset.url || asset.secure_url)) || extractMediaUrls(j)[0];
  if (!url) throw new Error('upload-asset returned no URL: ' + res.text.slice(0, 300));
  return url;
}

async function resolveModel(query) {
  await fetchDefinitions();
  const def = query ? findModelDef(query) : null;
  if (!def) {
    const names = collectModelDefs().map(d => d.data.name).filter(Boolean).slice(0, 12);
    throw Object.assign(new Error(`Unknown model "${query || ''}". Try one of: ${names.join(', ')}`), { status: 400 });
  }
  return def;
}

// GET /api/v1/users -> { email, name, activeWorkspace: { subscription: { credits, monthlyCredits, name, creditsList:[{allocatedAmount,available,expiresAt}] }, userWorkspaceCreditsUsage } }
async function getCredits() {
  const u = await api('/api/v1/users');
  const ws = (u && u.activeWorkspace) || {};
  const sub = ws.subscription || {};
  const slot = Array.isArray(sub.creditsList) && sub.creditsList[0] || {};
  return {
    email: u.email || null,
    name: u.name || null,
    plan: sub.name || sub.type || 'unknown',
    credits: sub.credits ?? ws.credits ?? null,
    allocated: slot.allocatedAmount ?? sub.monthlyCredits ?? null,
    usage: ws.userWorkspaceCreditsUsage ?? null,
    renews_at: slot.expiresAt || null,
    workspace: ws.workspaceName || null,
  };
}

function send(res, status, obj) {
  res.writeHead(status, { 'content-type': 'application/json', 'access-control-allow-origin': '*' });
  res.end(JSON.stringify(obj));
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'OPTIONS') {
    res.writeHead(204, { 'access-control-allow-origin': '*', 'access-control-allow-methods': 'GET, POST, OPTIONS', 'access-control-allow-headers': 'authorization, content-type' });
    return res.end();
  }
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks);
  if (PROXY_KEY && (req.headers.authorization || '') !== `Bearer ${PROXY_KEY}`) {
    return send(res, 401, { error: { message: 'Invalid proxy key', type: 'invalid_request_error' } });
  }

  try {
    if (req.method === 'GET' && req.url === '/healthz') {
      let session = 'unknown', credits = null;
      try { await api('/api/v1/models/prices'); session = 'authorized'; }
      catch (e) { session = /Not signed in/i.test(e.message) ? 'unauthorized' : 'error: ' + e.message.slice(0, 160); }
      if (session === 'authorized') { try { credits = (await getCredits()).credits; } catch { } }
      return send(res, 200, { status: 'ok', cdp: CDP_URL, page: page ? page.url() : null, session, credits, flow: primaryFlowId });
    }
    if (req.method === 'GET' && req.url === '/v1/credits') {
      return send(res, 200, { object: 'weavy_credits', ...(await getCredits()) });
    }
    if (req.method === 'GET' && req.url.startsWith('/v1/models')) {
      await fetchDefinitions();
      const data = collectModelDefs()
        .filter(d => d.metadata && Array.isArray(d.metadata.outputTypes) && d.metadata.outputTypes.includes('image'))
        .map(d => ({ id: d.data.name, object: 'model', owned_by: `${d.data.model.service}:${d.data.model.name}` }));
      return send(res, 200, { object: 'list', data });
    }
    if (req.method === 'POST' && req.url.startsWith('/v1/images/generations')) {
      const j = parseJson(raw.toString('utf8')) || {};
      if (!j.prompt) return send(res, 400, { error: { message: 'prompt is required', type: 'invalid_request_error' } });
      const def = await resolveModel(j.model);
      const size = mapSize(j.size, def);
      const { urls, credits } = await runModel(def, { prompt: String(j.prompt), n: j.n, ...size });
      return send(res, 200, { created: Math.floor(Date.now() / 1000), data: urls.map(u => ({ url: u })), weavy_credits_remaining: credits });
    }
    if (req.method === 'POST' && req.url.startsWith('/v1/images/edits')) {
      const parts = parseMultipart(raw, req.headers['content-type']);
      if (!parts) return send(res, 400, { error: { message: 'expected multipart/form-data', type: 'invalid_request_error' } });
      const imagePart = parts.find(p => p.name === 'image' && p.filename) || parts.find(p => p.filename);
      const promptPart = parts.find(p => p.name === 'prompt');
      const modelPart = parts.find(p => p.name === 'model');
      if (!imagePart) return send(res, 400, { error: { message: 'image file is required', type: 'invalid_request_error' } });
      const prompt = promptPart ? promptPart.data.toString('utf8') : '';
      if (!prompt) return send(res, 400, { error: { message: 'prompt is required', type: 'invalid_request_error' } });
      const def = await resolveModel(modelPart ? modelPart.data.toString('utf8') : 'Nano Banana 2.1');
      log('uploading edit image:', imagePart.filename, imagePart.data.length, 'bytes');
      const assetUrl = await uploadImageInPage(imagePart.data, imagePart.filename, imagePart.contentType);
      log('asset uploaded:', assetUrl);
      const { urls, credits } = await runModel(def, { prompt, imageUrl: assetUrl, n: 1 });
      return send(res, 200, { created: Math.floor(Date.now() / 1000), data: urls.map(u => ({ url: u })), weavy_credits_remaining: credits });
    }
    return send(res, 404, { error: { message: `No route: ${req.method} ${req.url}`, type: 'invalid_request_error' } });
  } catch (e) {
    log('ERROR:', e.message);
    return send(res, e.status === 401 ? 401 : e.status === 400 ? 400 : 502, { error: { message: e.message, type: 'api_error' } });
  }
});

connectBrowser()
  .then(() => {
    server.listen(PORT, '127.0.0.1', () => {
      log(`weavy-proxy listening on http://127.0.0.1:${PORT}/v1  (dynamic models via proxy-owned flows)`);
      log(`OpenAI base URL: http://127.0.0.1:${PORT}/v1   health: http://127.0.0.1:${PORT}/healthz`);
    });
  })
  .catch(e => { console.error('FATAL:', e.message); process.exit(1); });

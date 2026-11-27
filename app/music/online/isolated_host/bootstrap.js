/* B25 opt-in adapter based on the independently reviewed B23 bootstrap.
 * AppContainer, Job and the parent broker enforce confinement.
 */
const fs = require('fs');
const path = require('node:path');
const vm = require('node:vm');
const {TextDecoder} = require('node:util');
const readline = require('readline');
const { AsyncLocalStorage } = require('async_hooks');

const pluginPath = process.env.STARLIGHT_PLUGIN_PATH || '';
const pluginType = process.env.STARLIGHT_PLUGIN_TYPE || 'musicfree';
const pluginId = process.env.STARLIGHT_PLUGIN_ID || 'unknown';
const sourceId = process.env.STARLIGHT_SOURCE_ID || pluginId;
const origin = process.env.STARLIGHT_PLUGIN_ORIGIN || '';

const context = new AsyncLocalStorage();
const callId = /^[\da-f]{32}:[1-9]\d{0,15}$/;
const methods = new Set(['ping', 'listSources', 'searchPage', 'search', 'getUrl', 'downloadUrl', 'getLyric', 'getCover']);
const activeCalls = new Set();
const hostPending = new Map();
let hostSeq = 0;
let ready = false;
const frameLimit = 64 * 1024;

function record(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function send(obj) {
  const line = JSON.stringify({...obj, v: 1});
  if (Buffer.byteLength(line) >= frameLimit) throw new Error('PROTOCOL_OVERSIZED');
  process.stdout.write(line + '\n');
}

function hostHttp(request) {
  const owner = context.getStore();
  if (!activeCalls.has(owner) || hostPending.size >= 4) {
    return Promise.reject(new Error('HOST_HTTP_UNAVAILABLE'));
  }
  return new Promise((resolve, reject) => {
    const id = 'h' + (++hostSeq);
    const timer = setTimeout(() => {
      hostPending.delete(id);
      reject(new Error('HOST_HTTP_TIMEOUT'));
    }, 6500);
    hostPending.set(id, {resolve, reject, timer, owner});
    try {
      send({type: 'host_request', id, call_id: owner, request});
    } catch (error) {
      clearTimeout(timer);
      hostPending.delete(id);
      reject(error);
    }
  });
}

// Only host-verified files from the read-only package ACL are module candidates.
function readModules() {
  const root = process.env.STARLIGHT_PLUGIN_ROOT;
  const list = process.env.STARLIGHT_PLUGIN_FILES;
  if (!root || !list) throw new Error('PACKAGE_CONFIGURATION');
  const names = JSON.parse(fs.readFileSync(list, 'utf8'));
  if (!Array.isArray(names) || !names.length || names.length > 64 || new Set(names).size !== names.length) {
    throw new Error('PACKAGE_MODULE_LIST');
  }
  const modules = [];
  const decoder = new TextDecoder('utf-8', {fatal: true});
  const entry = path.relative(root, pluginPath).split(path.sep).join('/');
  for (const name of names) {
    if (typeof name !== 'string' || name.split('/').some(part =>
      !/^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$/.test(part) || part.endsWith('.'))) {
      throw new Error('PACKAGE_MODULE_NAME');
    }
    const kind = path.posix.extname(name);
    if (kind !== '.js' && kind !== '.json') continue;
    const filename = path.join(root, ...name.split('/'));
    if (!fs.lstatSync(filename).isFile()) throw new Error('PACKAGE_FILE_TYPE');
    const sourceBytes = fs.readFileSync(filename);
    if (sourceBytes.length > 1024 * 1024) throw new Error('PACKAGE_FILE_SIZE');
    const contents = decoder.decode(sourceBytes);
    modules.push({name, kind, contents});
  }
  if (!names.includes(entry) || !entry.endsWith('.js')) throw new Error('PACKAGE_ENTRY');
  return {entry, modules};
}

function createRuntime(config, entry, modules, createLx, bridge) {
  const factories = new Map(modules);
  const cache = new Map();
  const hostHttp = request => new Promise((resolve, reject) => {
    const value = JSON.stringify(request);
    bridge.request(value, (ok, response) => {
      if (ok) {
        try { resolve(JSON.parse(response)); } catch { reject(new Error('HOST_HTTP_FAILED')); }
      } else reject(new Error('HOST_HTTP_FAILED'));
    });
  });
  // Protocol errors retain structured codes. Plugin logs can contain signed URLs.
  const discard = () => {};
  globalThis.console = Object.freeze({log: discard, info: discard, error: discard, warn: discard, debug: discard});
  globalThis.hostHttp = hostHttp;
  globalThis.hostConfig = Object.freeze(config);

  function resolve(name, source) {
    if (typeof name !== 'string' || (!name.startsWith('./') && !name.startsWith('../'))) {
      throw new Error('REQUIRE_UNAVAILABLE');
    }
    const parts = source.split('/').slice(0, -1);
    for (const part of name.split('/')) {
      if (part === '.' || !part) continue;
      if (part === '..' && parts.length) parts.pop();
      else if (part === '..') throw new Error('REQUIRE_UNAVAILABLE');
      else parts.push(part);
    }
    const exact = parts.join('/');
    const found = [exact, exact + '.js', exact + '.json'].find(candidate => factories.has(candidate));
    if (!found) throw new Error('REQUIRE_UNAVAILABLE');
    return found;
  }

  function load(name) {
    if (cache.has(name)) return cache.get(name).exports;
    const factory = factories.get(name);
    if (!factory) throw new Error('PACKAGE_ENTRY');
    const module = {exports: {}};
    cache.set(name, module);
    try {
      factory(module, module.exports, dep => load(resolve(dep, name)));
      return module.exports;
    } catch (error) {
      cache.delete(name);
      throw error;
    }
  }

  const lx = config.pluginType === 'lx' ? createLx(hostHttp, new Set(config.providers)) : null;
  if (lx) globalThis.lx = lx.api;
  const exports = load(entry);
  if (lx) lx.finishRegistration();
  return {plugin: exports, lx, parse: JSON.parse};
}

function loadPackage() {
  const {entry, modules} = readModules();
  const config = JSON.parse(process.env.STARLIGHT_PLUGIN_CONFIG || '{}');
  if (!record(config)) throw new Error('PACKAGE_CONFIGURATION');
  const sandbox = vm.createContext(Object.create(null), {codeGeneration: {strings: false, wasm: false}});
  const factories = vm.runInContext('[]', sandbox);
  for (const {name, kind, contents} of modules) {
    const source = kind === '.json' ? 'module.exports = JSON.parse(' + JSON.stringify(contents) + ');'
      : '"use strict";\n' + contents;
    const factory = vm.compileFunction(source, ['module', 'exports', 'require'],
      {parsingContext: sandbox, filename: name});
    const pair = vm.runInContext('[]', sandbox);
    pair.push(name, factory);
    factories.push(pair);
  }
  const launch = new vm.Script(
    '((bridge, factories) => (' + createRuntime.toString() + ')(' +
    JSON.stringify({...config, origin, pluginId, sourceId, pluginType, providers: [...lxAllowedProviders()]}) +
    ',' + JSON.stringify(entry) + ',factories,(' + createLx.toString() + '),bridge))',
    {filename: 'verified-music-plugin.js'},
  );
  // VM limits the exposed application API. The Windows token and broker enforce
  // process security. No Node objects or native modules are injected here.
  const expose = fn => Object.freeze(Object.setPrototypeOf(fn, null));
  const bridge = Object.freeze(Object.assign(Object.create(null), {
    request: expose((raw, reply) => {
      if (typeof reply !== 'function') return;
      if (typeof raw !== 'string' || Buffer.byteLength(raw) > frameLimit) {
        reply(false, '');
        return;
      }
      let request;
      try { request = JSON.parse(raw); } catch { reply(false, ''); return; }
      void hostHttp(request).then(result => reply(true, JSON.stringify(result)), () => reply(false, ''))
        .catch(() => {});
    }),
  }));
  return launch.runInContext(sandbox)(bridge, factories);
}

function lxAllowedProviders() {
  const values = JSON.parse(process.env.STARLIGHT_LX_SOURCES || '[]');
  if (!Array.isArray(values) || values.length > 16 ||
      values.some(key => typeof key !== 'string' || !/^[a-zA-Z0-9_-]{1,64}$/.test(key))) {
    throw new Error('LX_PROVIDER_CONFIGURATION');
  }
  return new Set(values);
}

function createLx(hostHttp, providers) {
  const handlers = new Map();
  const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const allowed = ['flac24bit', 'flac', '320k', '128k'];
  const events = Object.freeze({request: 'request', inited: 'inited', updateAlert: 'updateAlert'});
  let registering = true;
  const capabilities = new Map();
  function on(name, handler) {
    if (!Object.hasOwn(events, name) || typeof handler !== 'function') throw new Error('LX_EVENT_UNAVAILABLE');
    if (!handlers.has(name)) handlers.set(name, []);
    const registered = handlers.get(name);
    if (registered.length >= 16) throw new Error('LX_HANDLER_LIMIT');
    registered.push(handler);
  }
  function off(name, handler) {
    const list = handlers.get(name) || [];
    const index = list.indexOf(handler);
    if (index >= 0) list.splice(index, 1);
  }
  async function dispatch(name, data) {
    return Promise.all((handlers.get(name) || []).map(handler => new Promise((resolve, reject) => {
      try {
        const result = handler(data, resolve);
        if (result && typeof result.then === 'function') result.then(resolve, reject);
        else if (result !== undefined) resolve(result);
      } catch (error) {
        reject(error);
      }
    })));
  }
  async function sendEvent(name, data) {
    if (name !== 'inited' || !registering || !record(data?.sources)) return;
    for (const source of providers) {
      if (!Object.hasOwn(data.sources, source)) continue;
      const value = data.sources[source];
      if (!record(value)) continue;
      capabilities.set(source, {
        actions: Array.isArray(value.actions) ? value.actions.filter(action =>
          action === 'musicUrl' || action === 'lyric' || action === 'pic') : [],
        qualitys: Array.isArray(value.qualitys) ? value.qualitys.filter(tier => allowed.includes(tier)) : [],
      });
    }
  }
  async function request(url, options = {}, callback) {
    let result;
    try {
      const res = await hostHttp({
        url, method: options.method || 'GET', headers: options.headers || {},
        body: options.body === undefined ? options.data : options.body,
      });
      let body = res.body;
      const type = Object.entries(res.headers || {}).find(([key]) => key.toLowerCase() === 'content-type')?.[1];
      if (typeof body === 'string' && typeof type === 'string' && /[/+]json(?:\s*;|$)/i.test(type)) {
        try { body = JSON.parse(body); } catch { /* Keep invalid JSON as text. */ }
      }
      result = {statusCode: res.status, headers: res.headers || {}, body};
    } catch (error) {
      if (typeof callback === 'function') callback(error, null);
      throw error;
    }
    if (typeof callback === 'function') callback(null, result);
    return result;
  }
  return {
    capabilities, dispatch, finishRegistration: () => { registering = false; },
    api: {
      on, off, send: sendEvent, request,
      utils: {
        string: {parseJson: JSON.parse, stringify: JSON.stringify},
        request,
      },
      env: {getUserVariables: () => ({}), platform: 'pc'},
      version: '2.0.0',
      EVENT_NAMES: events,
    },
  };
}

function nativeId(item) {
  const values = [item?.id, item?.songmid, item?.hash, item?.rid];
  const value = values.find(key => (typeof key === 'string' && key.length > 0 && key.length <= 512)
    || Number.isSafeInteger(key));
  return value === undefined ? '' : String(value);
}

// Adapted from formal B22 js_bridge/lib/mf_loader.js. Duration is seconds.
// Explicit milliseconds are accepted only when valid and agreeing.
function durationSeconds(item) {
  const valid = n => typeof n === 'number' && Number.isFinite(n) && n > 0 && n <= Number.MAX_SAFE_INTEGER;
  if (valid(item.duration)) return item.duration;
  const milliseconds = [item.durationMs, item.duration_ms].filter(valid);
  return milliseconds.length && new Set(milliseconds).size === 1 ? milliseconds[0] / 1000 : 0;
}

function mediaUrl(value) {
  if (typeof value !== 'string' || value.length > 16384 ||
      !/^https?:[/][/]/i.test(value) || /[\s\x00-\x1f\x7f\\]/u.test(value)) return null;
  try {
    const url = new URL(value);
    return url.hostname && !url.username && !url.password && url.port !== '0' ? value : null;
  } catch {
    return null;
  }
}

const reservedHeaders = new Set(['host', 'content-length', 'transfer-encoding', 'connection',
  'proxy-authorization', 'proxy-connection', 'upgrade', 'te', 'trailer', 'range', 'if-range']);
function mediaHeaders(value) {
  if (!record(value)) return null;
  const clean = Object.create(null);
  for (const [key, content] of Object.entries(value)) {
    const header = key.toLowerCase();
    if (/^[!#$%&'*+.^_\x60|~\dA-Za-z-]{1,64}$/.test(key) && !reservedHeaders.has(header) &&
        typeof content === 'string' && content.length <= 4096 && !/[\x00-\x1f\x7f]/u.test(content)) {
      clean[header] = content;
    }
  }
  return Object.keys(clean).length ? clean : null;
}

const rowFields = ['id', 'songmid', 'songId', 'songIdStr', 'musicId', 'hash', 'rid', 'strMediaMid', 'platform', 'source',
  'albumId', 'albumMid', 'isrc', 'ISRC', 'musicbrainzRecordingId', 'mbRecordingId', 'recording_mbid', 'version',
  'versionName', 'subtitle', 'versionType', 'mixName', 'interval'];
const qualityFields = ['size', 'sizeBytes', 'fileSize', 'format', 'bitrate', 'sampleRate', 'bitDepth', 'quality'];
const qualityNames = ['super', 'high', 'standard', 'low', 'flac24bit', 'flac', '320k', '128k', 'lossless', 'hires'];
function project(input, fields, limit) {
  return Object.fromEntries(fields.flatMap(key => {
    const value = input[key];
    if (typeof value === 'string' && value && value.length <= limit && !value.includes('://') &&
        !/^(?:[a-z]:[\\/]|[/\\])/i.test(value)) return [[key, value]];
    return typeof value === 'number' && Number.isFinite(value) ? [[key, value]] : [];
  }));
}

function normalizeTrack(item) {
  const artists = Array.isArray(item.artists)
    ? item.artists.map(a => record(a) ? a.name : a).filter(Boolean).join(' / ')
    : (item.artist || item.artists || '');
  const field = value => typeof value === 'string' ? value.slice(0, 512) : '';
  const id = nativeId(item);
  const title = field(item.title || item.name);
  const artist = field(artists);
  const album = field(record(item.album) ? item.album.title || item.album.name : item.album);
  const duration = durationSeconds(item);
  const raw = {id, title, artist, album};
  // Retain the native ID aliases used by protocol resolvers when the process
  // has been replaced and the full raw search row is absent.
  Object.assign(raw, project(item, rowFields, 512));
  if (duration) raw.duration = duration;
  if (record(item.qualities)) {
    raw.qualities = Object.fromEntries(qualityNames.flatMap(key => record(item.qualities[key])
      ? [[key, project(item.qualities[key], qualityFields, 80)]] : []));
  }
  return {
    source_name: sourceId,
    track_id: id,
    title, artist, album,
    duration_seconds: duration,
    extra: {_raw: raw},
  };
}

const MF_TIERS = ['super', 'high', 'standard', 'low'];
const LX_TIERS = ['flac24bit', 'flac', '320k', '128k'];

// B24 never persists plugin result URLs. Reuse bounded raw search rows inside
// this generation only. Resolvers that support native IDs can work after restart.
const searchCache = new Map();
function remember(item) {
  const id = nativeId(item);
  if (!id || id.length > 512) return;
  let serialized;
  try { serialized = JSON.stringify(item); } catch { return; }
  if (typeof serialized !== 'string') return;
  if (Buffer.byteLength(serialized) > 8192) return;
  searchCache.delete(id);
  searchCache.set(id, serialized);
  if (searchCache.size > 240) searchCache.delete(searchCache.keys().next().value);
}

function checkTrack(params, mustCarryRaw = false) {
  if (typeof params.track_id !== 'string' || !params.track_id || params.track_id.length > 512) return null;
  const cached = searchCache.get(params.track_id);
  if (cached) return parsePluginJSON(cached);
  if (record(params.raw_item)) {
    const item = params.raw_item.musicInfo || params.raw_item;
    if (nativeId(item) !== params.track_id) return null;
    return parsePluginJSON(JSON.stringify(params.raw_item));
  }
  return mustCarryRaw ? null : parsePluginJSON(JSON.stringify({id: params.track_id}));
}

function attachment(raw) {
  const url = mediaUrl(typeof raw === 'string' ? raw : raw?.url);
  return url ? {url, headers: mediaHeaders(raw?.headers)} : null;
}

function mediaResult(raw, tier, exact) {
  const url = mediaUrl(typeof raw === 'string' ? raw : raw?.url);
  if (!url) return null;
  const declared = typeof raw?.quality === 'string' ? raw.quality : null;
  if (exact && declared && declared !== tier) return null;
  return {
    url, quality: declared || tier, source_quality: declared,
    requested_quality: tier, headers: mediaHeaders(raw?.headers),
  };
}

async function resolveMusicFree(method, params) {
  const hasGetter = typeof plugin.getMediaSource === 'function';
  const item = checkTrack(params, !hasGetter);
  if (!item) return null;
  if (!hasGetter) {
    if (method === 'downloadUrl' && params.quality !== null) return null;
    const url = mediaUrl(item.url);
    return url ? {url, direct_url: true, supports_renditions: false} : null;
  }
  const choices = renditionChoices(method, params, MF_TIERS, 'high', ['high', 'standard', 'low']);
  return tryRenditions(choices, async tier => plugin.getMediaSource(item, tier),
    method === 'downloadUrl' || Array.isArray(params.playback_qualities));
}

function renditionChoices(method, params, allowed, defaultTier, fallback) {
  if (method === 'downloadUrl') return allowed.includes(params.quality) ? [params.quality] : [];
  if (Array.isArray(params.playback_qualities)) return [...new Set(params.playback_qualities)].filter(tier => allowed.includes(tier));
  return [...new Set([allowed.includes(params.quality) ? params.quality : defaultTier, ...fallback])];
}

async function tryRenditions(choices, fetchRendition, exact) {
  for (const [index, tier] of choices.entries()) {
    try {
      const value = mediaResult(await fetchRendition(tier), tier, exact);
      if (value) return index ? {...value, downgraded: true} : value;
    } catch { /* One unavailable rendition does not preclude subsequent allowed renditions. */ }
  }
  return null;
}

async function invoke(method, params) {
  if (method === 'ping') return {pong: true, pid: process.pid, type: pluginType, capabilities};
  if (method === 'listSources') return {sources: [{name: sourceId, type: pluginType, origin}]};
  if (params.source !== undefined && params.source !== sourceId) {
    if (method === 'searchPage' || method === 'search') return {status: 'unavailable', page: params.page};
    return null;
  }
  const media = method === 'getUrl' || method === 'downloadUrl';
  if (pluginType === 'musicfree') {
    if (method === 'searchPage' || method === 'search') {
      const page = method === 'search' ? 1 : params.page;
      if (!Number.isSafeInteger(page) || page < 1) throw new Error('STRUCTURE_PAGE');
      if (typeof plugin.search !== 'function') return {status: 'unavailable', page};
      const keyword = typeof params.keyword === 'string' ? params.keyword.trim() : '';
      if (!keyword) throw new Error('STRUCTURE_KEYWORD');
      const raw = await plugin.search(keyword, page, 'music');
      const data = Array.isArray(raw) ? raw : raw?.data;
      const items = Array.isArray(data) && data.filter(record);
      if (!items || (data.length && !items.length)) throw new Error('STRUCTURE_SEARCH');
      if (items.length > 120) throw new Error('PROTOCOL_OVERSIZED');
      const rows = items;
      rows.forEach(remember);
      return {status: 'ok', page, source: sourceId, tracks: rows.map(normalizeTrack),
        is_end: typeof raw.isEnd === 'boolean' ? raw.isEnd : null,
        total: Number.isSafeInteger(raw.total) && raw.total >= 0 ? raw.total : null};
    }
    if (media) return resolveMusicFree(method, params);
    const item = checkTrack(params);
    if (!item) return null;
    if (method === 'getCover') {
      const getter = plugin.getCover || plugin.getPic;
      return attachment((typeof getter === 'function' ? await getter.call(plugin, item) : null)
        || item.artwork || item.image || item.cover || item.coverImg);
    }
    if (typeof plugin.getLyric !== 'function') return null;
    const result = await plugin.getLyric(item);
    const remote = attachment(result);
    if (remote) return remote;
    const lyric = typeof result === 'string' ? result : result?.lyric || result?.rawLrc;
    return typeof lyric === 'string' ? {lyric} : null;
  }
  if (!media && method !== 'getLyric' && method !== 'getCover') return {status: 'unavailable', page: params.page};
  const raw = checkTrack(params, true);
  const item = raw?.musicInfo || raw;
  const provider = item?.source || raw?.source;
  const supported = lx.capabilities.get(provider);
  if (!item || !supported) return null;
  const dispatch = data => lx.dispatch('request', parsePluginJSON(JSON.stringify(data)));
  if (method === 'getCover') {
    if (!supported.actions.includes('pic')) return null;
    const [result] = await dispatch({action: 'pic', source: provider, info: {...raw, musicInfo: item}});
    return attachment(result);
  }
  if (!media) {
    if (!supported.actions.includes('lyric')) return null;
    const [result] = await dispatch({action: 'lyric', source: provider,
      info: {...raw, musicInfo: item}});
    const remote = attachment(result);
    if (remote) return remote;
    return typeof result === 'string' ? {lyric: result} :
      result?.lyric || result?.lxlyric ? {lyric: result.lyric || '', lxlyric: result.lxlyric || null} : null;
  }
  if (!supported.actions.includes('musicUrl')) return null;
  const choices = renditionChoices(method, params, LX_TIERS, '320k', []).filter(tier => supported.qualitys.includes(tier));
  return tryRenditions(choices, async tier => {
    const [result] = await dispatch({action: 'musicUrl', source: provider,
      info: {type: tier, musicInfo: item}, quality: tier});
    return result;
  }, method === 'downloadUrl' || Array.isArray(params.playback_qualities));
}

let plugin;
let lx;
let parsePluginJSON;
let capabilities;

function boot() {
  if (!pluginPath) throw new Error('PLUGIN_PATH_MISSING');
  ({plugin, lx, parse: parsePluginJSON} = loadPackage());
  if (pluginType === 'musicfree') {
    if (!record(plugin)) throw new Error('PLUGIN_EXPORT');
    const resolveMedia = typeof plugin.getMediaSource === 'function';
    capabilities = {search: typeof plugin.search === 'function', url: resolveMedia, resolve_media: resolveMedia,
      direct_url: !resolveMedia, lyric: typeof plugin.getLyric === 'function'};
  } else if (pluginType === 'lx') {
    const advertised = [...lx.capabilities.values()];
    capabilities = {search: false, url: advertised.some(info => info.actions.includes('musicUrl')),
      lyric: advertised.some(info => info.actions.includes('lyric')),
      providers: [...lx.capabilities.keys()],
      provider_qualities: Object.fromEntries([...lx.capabilities].map(([provider, info]) => [provider, info.qualitys]))};
  } else {
    throw new Error('PLUGIN_TYPE_UNKNOWN');
  }
  ready = true;
  send({type: 'ready', pid: process.pid, plugin_id: pluginId, plugin_type: pluginType,
        source_name: sourceId, origin, capabilities});
}

const rl = readline.createInterface({input: process.stdin, terminal: false});
rl.on('line', async line => {
  if (Buffer.byteLength(line) >= frameLimit) return;
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (!ready || !record(msg) || msg.v !== 1) return;
  if (msg.type === 'host_response') {
    const box = hostPending.get(msg.id);
    if (!box || msg.call_id !== box.owner || typeof msg.ok !== 'boolean') return;
    hostPending.delete(msg.id);
    clearTimeout(box.timer);
    if (msg.ok) box.resolve(msg.result);
    else box.reject(new Error('HOST_HTTP_ERROR'));
    return;
  }
  if (msg.type !== 'call' || typeof msg.id !== 'string' || !callId.test(msg.id) ||
      !record(msg.params) || !methods.has(msg.method) || activeCalls.has(msg.id)) return;
  if (activeCalls.size >= 4) {
    send({type: 'result', id: msg.id, ok: false, error: 'TOO_MANY_PENDING'});
    return;
  }
  activeCalls.add(msg.id);
  try {
    const result = await context.run(msg.id, () => invoke(msg.method, msg.params));
    send({type: 'result', id: msg.id, ok: true, result});
  } catch (error) {
    const message = ['PROTOCOL_OVERSIZED', 'STRUCTURE_SEARCH', 'STRUCTURE_KEYWORD', 'STRUCTURE_PAGE']
      .includes(error?.message) ? error.message : 'PLUGIN_FAILED';
    send({type: 'result', id: msg.id, ok: false, error: message});
  } finally {
    activeCalls.delete(msg.id);
    for (const [id, box] of hostPending) {
      if (box.owner !== msg.id) continue;
      clearTimeout(box.timer);
      hostPending.delete(id);
      box.reject(new Error('CALL_FINISHED'));
    }
  }
});

rl.on('close', () => {
  for (const box of hostPending.values()) clearTimeout(box.timer);
  hostPending.clear();
  process.exit(process.exitCode || 0);
});

try {
  boot();
} catch {
  send({type: 'ready', ok: false, error: 'PLUGIN_START_FAILED'});
  process.exitCode = 2;
  rl.close();
}

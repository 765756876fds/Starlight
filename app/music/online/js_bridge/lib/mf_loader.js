// MusicFree plugin loader.
const path = require('path');
const fs = require('fs');
const vm = require('vm');

function load(filePath) {
  if (typeof globalThis.env === 'undefined') globalThis.env = {};
  if (typeof globalThis.log === 'undefined') globalThis.log = console.log;
  if (typeof globalThis.onError === 'undefined') globalThis.onError = (e) => { throw e; };
  delete require.cache[require.resolve(filePath)];

  // Preprocess: MusicFree plugins sometimes write `let z = z || "..."` which
  // is a TDZ self-reference that only works in their Hiker/Electron loader.
  // Rewrite to `var z = (typeof z !== 'undefined' && z) ? z : ...`.
  let src = fs.readFileSync(filePath, 'utf8');
  src = src.replace(/\blet\s+([a-zA-Z_$][\w$]*)\s*=\s*\1\s*\|\|\s*([^;]+);/g,
    'var $1 = (typeof $1 !== "undefined" && $1) ? $1 : $2;');
  // Also handle `let t, z = z || ...`
  src = src.replace(/\blet\s+([a-zA-Z_$][\w$]*)\s*,\s*([a-zA-Z_$][\w$]*)\s*=\s*\2\s*\|\|\s*([^;]+);/g,
    'let $1; var $2 = (typeof $2 !== "undefined" && $2) ? $2 : $3;');

  const module = { exports: {} };
  const sandbox = {
    module, exports: module.exports,
    require: (name) => require(name),
    console, process, Buffer,
    global: globalThis,
    env: { prod: false, dev: false, getUserVariables: () => ({}) },
    log: console.log,
    onError: (e) => { throw e; },
    __dirname: path.dirname(filePath),
    __filename: filePath,
  };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: filePath });
  const mod = module.exports;
  if (!mod || typeof mod !== 'object') throw new Error('MusicFree plugin did not export object');
  const sourceName = mod.platform || path.basename(filePath, '.js');
  // Attach user variables (from env MF_USER_VARIABLES) if plugin declares them.
  try {
    const uvRaw = process.env.MF_USER_VARIABLES;
    if (uvRaw) {
      const all = JSON.parse(uvRaw);
      const my = all[sourceName] || all[path.basename(filePath, '.js')] || {};
      if (typeof mod.userVariables === 'object' && mod.userVariables !== null) {
        Object.assign(mod.userVariables, my);
      } else {
        mod.userVariables = my;
      }
    }
  } catch (e) {
    console.error('[mf_loader] userVariables apply fail:', e.message);
  }
  return {
    name: sourceName,
    type: 'musicfree',
    search: mod.search,
    getMediaSource: mod.getMediaSource,
    getLyric: mod.getLyric,
    raw: mod,
  };
}

async function callSearch(plugin, keyword, limit) {
  if (typeof plugin.search !== 'function') return [];
  const result = await plugin.search(keyword, 1, 'music');
  const items = (result && result.data) || (Array.isArray(result) ? result : []);
  return items.slice(0, limit).map((item) => ({
    source_name: plugin.name,
    track_id: String(item.id || item.songmid || item.hash || ''),
    title: item.title || '',
    artist: item.artist || '',
    album: item.album || '',
    duration_seconds: item.duration ? Math.floor(Number(item.duration) / 1000) : 0,
    extra: { _raw: item },
  }));
}

async function callGetUrl(plugin, rawItem, quality) {
  if (typeof plugin.getMediaSource !== 'function') return null;
  const item = rawItem || { id: '' };
  const result = await plugin.getMediaSource(item, quality || '320k');
  if (!result) return null;
  if (typeof result === 'string') return { url: result, quality };
  const out = { url: result.url, quality: result.quality || quality };
  // 透传插件返回的 headers（如开心汽水给 douyin Referer），供播放链设置
  if (result.headers && typeof result.headers === 'object'
      && Object.keys(result.headers).length) {
    out.headers = result.headers;
  }
  return out;
}

async function callGetLyric(plugin, rawItem) {
  if (typeof plugin.getLyric !== 'function') return null;
  const item = rawItem || { id: '' };
  const result = await plugin.getLyric(item);
  if (!result) return null;
  if (typeof result === 'string') return { lyric: result };
  if (result.lyric) return { lyric: result.lyric };
  return null;
}

module.exports = { load, callSearch, callGetUrl, callGetLyric };

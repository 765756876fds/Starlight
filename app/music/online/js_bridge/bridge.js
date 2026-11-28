#!/usr/bin/env node
// JS music source bridge: MusicFree + LX Music.
// NDJSON over stdio.
const fs = require('fs');
const path = require('path');
const mfLoader = require('./lib/mf_loader');
const lxLoader = require('./lib/lx_loader');
const { startRpc } = require('./lib/jsonrpc');

process.on('unhandledRejection', (e) => {
  console.error('[bridge] unhandledRejection:', (e && e.message) || e);
});
process.on('uncaughtException', (e) => {
  console.error('[bridge] uncaughtException:', (e && e.message) || e);
});

// Keep plugin console output off the NDJSON protocol stream.
console.log = console.info = console.debug = console.error.bind(console);
const sources = new Map(); // name -> plugin object
const downloadOwners = new Map(); // canonical names only; duplicate owners are rejected
const activeDownloads = new Set();
const maxActiveDownloads = 4;

// Bound discovery reads independently. This cannot cancel plugin HTTP requests;
// it only prevents one stalled plugin from hiding other completed catalogues.
async function readCatalogue(read, timeoutMs = 8000) {
  let timer;
  try {
    return await Promise.race([
      Promise.resolve().then(read),
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          const error = new Error('Catalogue request timed out');
          error.catalogueTimeout = true;
          reject(error);
        }, timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

// One unresolved plugin call per source, including calls whose RPC timed out.
const activeSearches = new Map();
const maxActiveSearches = 12;
let peakSearches = 0;

async function searchPage(plugin, keyword, page) {
  if (plugin?.type !== 'musicfree' || typeof plugin.search !== 'function') {
    return { status: 'unavailable' };
  }
  if (!Number.isSafeInteger(page) || page < 1 || typeof keyword !== 'string' || !keyword.trim()) {
    return { status: 'structure' };
  }
  if (activeSearches.has(plugin) || activeSearches.size >= maxActiveSearches) {
    return { status: 'busy' };
  }
  const pending = Promise.resolve().then(() => mfLoader.callSearchPage(plugin, keyword, page));
  activeSearches.set(plugin, pending);
  peakSearches = Math.max(peakSearches, activeSearches.size);
  const release = () => activeSearches.delete(plugin);
  pending.then(release, release);
  try {
    return { status: 'ok', page, ...await readCatalogue(() => pending) };
  } catch (error) {
    const status = error?.catalogueTimeout ? 'timeout' :
      error?.category === 'structure' ? 'structure' : 'plugin';
    console.error(`[bridge] search failed source=${plugin.name} type=${status}`);
    return { status };
  }
}

function scanDir(dir, isLx) {
  if (!dir) return [];
  dir = path.resolve(dir);
  if (!fs.existsSync(dir)) return [];
  const out = [];
  for (const f of fs.readdirSync(dir)) {
    if (!f.endsWith('.js')) continue;
    const fp = path.join(dir, f);
    out.push({ fp, isLx });
  }
  return out;
}

async function loadAll() {
  const mfDir = process.env.MF_PLUGIN_DIR || '';
  const lxDir = process.env.LX_PLUGIN_DIR || '';
  const jobs = [
    ...scanDir(mfDir, false),
    ...scanDir(lxDir, true),
  ];
  const failures = [];
  for (const j of jobs) {
    try {
      const p = j.isLx ? await lxLoader.load(j.fp) : mfLoader.load(j.fp);
      p.filePath = j.fp;   // 记录本地路径（插件更新/替换用）
      sources.set(p.name, p);
      downloadOwners.set(p.name, downloadOwners.has(p.name) ? null : p);
      // aliases: basename (without .js) and lowercase
      const base = path.basename(j.fp, '.js');
      sources.set(base, p);
      sources.set(base.toLowerCase(), p);
      console.error(`[bridge] loaded ${j.isLx ? 'lx' : 'mf'}: ${p.name} (alias=${base})`);
    } catch (e) {
      failures.push({
        name: path.basename(j.fp, '.js'),
        type: j.isLx ? 'lx' : 'musicfree',
        file: j.fp,
        status: 'fail',
        error: String((e && e.message) || e),
      });
      console.error(`[bridge] load fail ${j.fp}: ${e.message}`);
    }
  }
  return failures;
}

async function dispatch(method, params) {
  switch (method) {
    case 'ping':
      return { pong: true };
    case 'listSources':
      return Array.from(sources.keys());
    case 'search': {
      const { source, keyword, limit } = params;
      const plugins = [...new Set(source === '*' ? sources.values() : [sources.get(source)])]
        .filter(p => p && p.type === 'musicfree');
      const out = [];
      const errors = {};
      let successful = 0;
      const results = await Promise.all(plugins.map(p => searchPage(p, keyword, 1)));
      for (const [index, result] of results.entries()) {
        if (result.status === 'ok') {
          out.push(...result.tracks.slice(0, limit || 20));
          successful += 1;
        } else {
          errors[plugins[index].name] = result.status;
        }
      }
      if (!successful && !Object.keys(errors).length) errors.js_bridge = 'unavailable';
      return { tracks: out, errors, successful };
    }
    case 'searchPage':
      return searchPage(sources.get(params.source), params.keyword, params.page);
    case 'searchStats':
      return { active: activeSearches.size, peak: peakSearches, limit: maxActiveSearches };
    case 'downloadUrl': {
      const { source, quality, track_id, raw_item, timeout_ms } = params;
      const p = downloadOwners.get(source);
      if (!p || p !== sources.get(source) || typeof track_id !== 'string' || !track_id) return null;
      const direct = p.type === 'musicfree' && typeof p.getMediaSource !== 'function';
      const tiers = p.type === 'musicfree' ? (direct ? [null] : ['super', 'high', 'standard', 'low']) :
        ['flac24bit', 'flac', '320k', '128k'];
      if (!tiers.includes(quality) || activeDownloads.has(p)
          || activeDownloads.size >= maxActiveDownloads) return null;
      const raw = raw_item && typeof raw_item === 'object' && !Array.isArray(raw_item) ? raw_item : null;
      const item = raw?.musicInfo || raw;
      const nativeId = item && String(item.id || item.songmid || item.hash || item.rid || '');
      if ((direct && !nativeId) || (nativeId && nativeId !== track_id)) return null;
      // LX requires an owned provider and the original musicInfo payload.
      if (p.type !== 'musicfree' && (!nativeId || !(item?.source || raw?.source))) return null;
      const deadline = Number.isSafeInteger(timeout_ms) ? Math.max(1, Math.min(6500, timeout_ms)) : 2000;
      const pending = Promise.resolve().then(() => p.type === 'musicfree'
        ? mfLoader.callGetUrl(p, raw || { id: track_id }, quality, true)
        : p.getUrl(source, raw, quality, true));
      activeDownloads.add(p);
      const release = () => activeDownloads.delete(p);
      pending.then(release, release);
      try {
        const result = await readCatalogue(() => pending, deadline);
        if (typeof result?.url !== 'string' || !result.url.trim()) return null;
        return {
          url: result.url, headers: result.headers || null, requested_quality: quality,
          source_quality: typeof result.source_quality === 'string' ? result.source_quality : null,
          ...(result.direct_url ? {direct_url: true} : {}),
        };
      } catch {
        return null;
      }
    }
    case 'getUrl': {
      const { source, quality, raw_item } = params;
      const p = sources.get(source);
      if (!p) throw new Error(`unknown source ${source}`);
      if (p.type === 'musicfree' && typeof p.getMediaSource !== 'function') {
        const nativeId = raw_item && String(raw_item.id || raw_item.songmid || raw_item.hash || raw_item.rid || '');
        if (!nativeId || nativeId !== params.track_id || downloadOwners.get(source) !== p) return null;
        return await mfLoader.callGetUrl(p, raw_item, null);
      }
      if (Array.isArray(params.playback_qualities)) {
        const allowed = p.type === 'musicfree' ? ['super','high','standard','low'] : ['flac24bit','flac','320k','128k'];
        const choices = [...new Set(params.playback_qualities)].filter(key => allowed.includes(key));
        const deadline = Date.now() + 6500;
        for (const [index, key] of choices.entries()) {
          if (Date.now() >= deadline) break;
          try {
            const result = p.type === 'musicfree'
              ? await mfLoader.callGetUrl(p, raw_item, key, true)
              : await p.getUrl(source, raw_item || {hash: params.track_id}, key, true);
            if (result && result.url) {
              if (p.type !== 'musicfree') {
                delete result.kuwo_rid;
                delete result.playback_metadata;
              }
              return {...result, requested_quality: key, downgraded: index > 0};
            }
          } catch { /* Try the next supported rendition of the same recording. */ }
        }
        return null;
      }
      if (p.type === 'musicfree') {
        const r = await mfLoader.callGetUrl(p, raw_item, quality);
        return r;
      } else {
        const musicInfo = raw_item || { hash: params.track_id };
        const r = await p.getUrl(source, musicInfo, quality);
        if (r && (r.kuwo_rid || r.playback_metadata)) {
          const {kuwo_rid, playback_metadata, ...result} = r;
          return result;
        }
        return r;
      }
    }
    case 'getLyric': {
      const { source, raw_item } = params;
      const p = sources.get(source);
      if (!p) throw new Error(`unknown source ${source}`);
      if (p.type === 'musicfree') {
        return await mfLoader.callGetLyric(p, raw_item);
      } else {
        return await p.getLyric(source, raw_item || { id: params.track_id });
      }
    }
    case 'getTopLists': {
      // 插件自报榜单能力：能提供的返回分组，不能的跳过（不硬编码哪个平台能做）。
      const seen = new Set();
      const out = [];
      const errors = {};
      const plugins = Array.from(sources.values()).filter(p => {
        if (seen.has(p)) return false;
        seen.add(p);
        return (!params.source || p.name === params.source) &&
          p.type === 'musicfree' && typeof p.getTopLists === 'function';
      });
      const results = await Promise.all(plugins.map(async p => {
        try {
          const groups = await readCatalogue(() => mfLoader.callGetTopLists(p));
          return { source: p.name, groups };
        } catch (e) {
          errors[p.name] = e.catalogueTimeout ? 'timeout' : 'plugin';
          console.error(`[bridge] getTopLists ${p.name} fail: ${e.message}`);
          return null;
        }
      }));
      for (const result of results) {
        if (result && result.groups && result.groups.length) out.push(result);
      }
      return { groups: out, errors };
    }
    case 'browse': {
      const plugin = sources.get(params.source);
      if (!plugin || plugin.type !== 'musicfree') throw new Error('Discovery source unavailable');
      return await mfLoader.browse(plugin, params.operation, params);
    }
    case 'getTopListDetail': {
      const { source, list_id, page } = params;
      const p = sources.get(source);
      if (!p) throw new Error(`unknown source ${source}`);
      if (p.type !== 'musicfree') throw new Error(`source ${source} not musicfree`);
      return await mfLoader.callGetTopListDetail(p, { id: list_id }, page || 1, params.limit);
    }
    case 'checkUpdates': {
      // 遍历 MusicFree 插件（按对象去重，忽略别名 key）→ 拉更新源取远端版本
      const seen = new Set();
      const out = [];
      for (const p of sources.values()) {
        if (seen.has(p)) continue;
        seen.add(p);
        if (p.type !== 'musicfree') continue;
        const meta = await mfLoader.fetchUpdateMeta(p, params.timeout_ms || 10000);
        out.push({ name: p.name, ...meta });
      }
      return out;
    }
    default:
      throw new Error('unknown method ' + method);
  }
}

(async () => {
  const failures = await loadAll();
  // 结构化插件列表：每个插件对象一条（Map 有 3 个别名 key → 按对象去重），
  // 加载失败的单独收集（带 error）。
  const seen = new Set();
  const plugins = [];
  for (const p of sources.values()) {
    if (seen.has(p)) continue;
    seen.add(p);
    plugins.push({ name: p.name, type: p.type, status: 'ok', file: p.filePath,
      version: p.type === 'musicfree' && typeof p.raw?.version === 'string' ? p.raw.version : null,
      capabilities: {
        ...(p.type === 'musicfree' ? {...mfLoader.capabilities(p),
          resolve_media: typeof p.getMediaSource === 'function'} : {}),
        search: p.type === 'musicfree' && typeof p.search === 'function',
      } });
  }
  for (const f of failures) plugins.push(f);
  // ready event
  process.stdout.write(JSON.stringify({
    id: '_ready', ok: true,
    sources: Array.from(sources.keys()),
    plugins,
  }) + '\n');
  startRpc(dispatch);
})();

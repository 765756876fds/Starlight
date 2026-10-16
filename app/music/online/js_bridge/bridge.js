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

const sources = new Map(); // name -> plugin object

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
      const want = source === '*' ? Array.from(sources.keys()) : [source];
      const out = [];
      for (const name of want) {
        const p = sources.get(name);
        if (!p || p.type !== 'musicfree') continue;
        try {
          const items = await mfLoader.callSearch(p, keyword, limit || 20);
          out.push(...items);
        } catch (e) {
          console.error(`[bridge] search ${name} fail: ${e.message}`);
        }
      }
      return out;
    }
    case 'getUrl': {
      const { source, quality, raw_item } = params;
      const p = sources.get(source);
      if (!p) throw new Error(`unknown source ${source}`);
      if (p.type === 'musicfree') {
        const r = await mfLoader.callGetUrl(p, raw_item, quality);
        return r;
      } else {
        const musicInfo = raw_item || { hash: params.track_id };
        const r = await p.getUrl(source, musicInfo, quality);
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
        return await p.getLyric(source, raw_item || {});
      }
    }
    case 'getTopLists': {
      // 插件自报榜单能力：能提供的返回分组，不能的跳过（不硬编码哪个平台能做）。
      const seen = new Set();
      const out = [];
      for (const p of sources.values()) {
        if (seen.has(p)) continue;
        seen.add(p);
        if (p.type !== 'musicfree' || typeof p.getTopLists !== 'function') continue;
        try {
          const groups = await mfLoader.callGetTopLists(p);
          if (groups && groups.length) out.push({ source: p.name, groups });
        } catch (e) {
          console.error(`[bridge] getTopLists ${p.name} fail: ${e.message}`);
        }
      }
      return out;
    }
    case 'getTopListDetail': {
      const { source, list_id, page } = params;
      const p = sources.get(source);
      if (!p) throw new Error(`unknown source ${source}`);
      if (p.type !== 'musicfree') throw new Error(`source ${source} not musicfree`);
      const r = await mfLoader.callGetTopListDetail(p, { id: list_id }, page || 1);
      return r || { is_end: true, tracks: [] };
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
    plugins.push({ name: p.name, type: p.type, status: 'ok', file: p.filePath });
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

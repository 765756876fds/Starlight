// MusicFree plugin loader.
const path = require('path');
const fs = require('fs');
const vm = require('vm');
const https = require('https');

// 预处理 + 沙箱执行插件源码，返回导出模块对象。
// 与 load() 共用：从文件读 + 预处理 + vm 沙箱（依赖从 js_bridge node_modules 解析）。
function loadFromSource(src, filePath) {
  src = src.replace(/\blet\s+([a-zA-Z_$][\w$]*)\s*=\s*\1\s*\|\|\s*([^;]+);/g,
    'var $1 = (typeof $1 !== "undefined" && $1) ? $1 : $2;');
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
  return mod;
}

function load(filePath) {
  if (typeof globalThis.env === 'undefined') globalThis.env = {};
  if (typeof globalThis.log === 'undefined') globalThis.log = console.log;
  if (typeof globalThis.onError === 'undefined') globalThis.onError = (e) => { throw e; };
  delete require.cache[require.resolve(filePath)];

  const src = fs.readFileSync(filePath, 'utf8');
  const mod = loadFromSource(src, filePath);
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
    getTopLists: mod.getTopLists,
    getTopListDetail: mod.getTopListDetail,
    raw: mod,
  };
}

// GET 文本（跟随 301/302/303/307/308 重定向，最多 redirects 跳）。
function fetchText(url, timeoutMs = 10000, redirects = 3) {
  return new Promise((resolve, reject) => {
    const doGet = (u, depth) => {
      const uu = new URL(u);
      const req = https.get({
        hostname: uu.hostname,
        path: uu.pathname + uu.search,
        headers: { 'User-Agent': 'lol-music-agent' },
      }, (res) => {
        if ([301, 302, 303, 307, 308].includes(res.statusCode)
            && res.headers.location && depth < redirects) {
          res.resume();
          const next = new URL(res.headers.location, uu).toString();
          return doGet(next, depth + 1);
        }
        let buf = '';
        res.on('data', (c) => { buf += c; });
        res.on('end', () => resolve({ status: res.statusCode, text: buf }));
      });
      req.setTimeout(timeoutMs, () => req.destroy(new Error('timeout')));
      req.on('error', reject);
    };
    doGet(url, 0);
  });
}

// 拉取插件更新源 → 沙箱加载新版 → 返回元信息（不判断版本，判断在 Python 侧）。
// 返回 {file, localVersion, remoteVersion, newCode} 或 {error}。
async function fetchUpdateMeta(plugin, timeoutMs = 10000) {
  const srcUrl = plugin && plugin.raw && plugin.raw.srcUrl;
  const filePath = plugin && plugin.filePath;
  const file = filePath ? path.basename(filePath) : '?';
  if (!srcUrl) return { file, error: 'no-src' };
  let res;
  try {
    res = await fetchText(srcUrl, timeoutMs);
  } catch (e) {
    return { file, error: 'fetch-' + String((e && e.message) || e).slice(0, 60) };
  }
  if (res.status !== 200) return { file, error: 'http-' + res.status };
  let remoteMod;
  try {
    remoteMod = loadFromSource(res.text, filePath || path.join(process.cwd(), file));
  } catch (e) {
    return { file, error: 'parse-' + String((e && e.message) || e).slice(0, 60) };
  }
  return {
    file,
    localVersion: (plugin.raw && plugin.raw.version) || null,
    remoteVersion: remoteMod.version || null,
    newCode: res.text,
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
  const want = quality || 'high';
  // 逐级回退：want → high → standard → low。MusicFree 插件标准音质键是
  // low/standard/high/super（'320k' 不是标准键，kg/wy 会 qualities['320k']
  // 查不到直接 return null）；super 失败补 high（避免直接跳 standard，下载/播放
  // 都想要尽量高的音质）；部分歌曲只有低档/VIP 限制，回退保证能播。
  for (const q of [...new Set([want, 'high', 'standard', 'low'])]) {
    try {
      const result = await plugin.getMediaSource(item, q);
      if (!result) continue;
      if (typeof result === 'string') return { url: result, quality: q };
      if (result.url) {
        const out = { url: result.url, quality: result.quality || q };
        // 透传插件返回的 headers（如开心汽水给 douyin Referer），供播放链设置
        if (result.headers && typeof result.headers === 'object'
            && Object.keys(result.headers).length) {
          out.headers = result.headers;
        }
        return out;
      }
    } catch (e) {
      continue; // 该档失败 → 试下一档
    }
  }
  return null;
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

// 榜单列表：插件自报能力（无 getTopLists → 返回 null，调用方跳过该插件）。
// MusicFree 标准：getTopLists() → [{title, data: [toplistItem]}]；
// toplistItem: {id, title, artwork/coverImg, ...}。规范化成 Python 友好结构。
async function callGetTopLists(plugin) {
  if (typeof plugin.getTopLists !== 'function') return null;
  const result = await plugin.getTopLists();
  const groups = Array.isArray(result) ? result : [];
  const out = [];
  for (const g of groups) {
    const lists = Array.isArray(g && g.data) ? g.data.map((it) => ({
      id: String((it && it.id !== undefined && it.id !== null) ? it.id : ''),
      title: (it && (it.title || it.name)) || '',
      cover_img: (it && (it.artwork || it.coverImg)) || '',
    })).filter((it) => it.id !== '') : [];
    if (lists.length) out.push({ title: (g && g.title) || '', lists });
  }
  return out;
}

// 榜单详情：MusicFree 标准 getTopListDetail(top, page) → {musicList, isEnd}。
// musicItem 与搜索条目同构 → 复用 callSearch 的规范化形状，Python 侧直接 _parse_tracks。
async function callGetTopListDetail(plugin, topListItem, page) {
  if (typeof plugin.getTopListDetail !== 'function') return null;
  const top = topListItem || { id: '' };
  const result = await plugin.getTopListDetail(top, page || 1);
  if (!result) return null;
  const items = (result && result.musicList) || (Array.isArray(result) ? result : []);
  return {
    is_end: !!(result && result.isEnd),
    tracks: items.map((item) => ({
      source_name: plugin.name,
      track_id: String(item.id || item.songmid || item.hash || ''),
      title: item.title || '',
      artist: item.artist || '',
      album: item.album || '',
      duration_seconds: item.duration ? Math.floor(Number(item.duration) / 1000) : 0,
      extra: { _raw: item },
    })),
  };
}

module.exports = { load, loadFromSource, fetchText, fetchUpdateMeta, callSearch, callGetUrl, callGetLyric, callGetTopLists, callGetTopListDetail };

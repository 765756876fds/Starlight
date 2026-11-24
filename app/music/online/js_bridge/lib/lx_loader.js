// LX Music source loader.
const path = require('path');
const { createLxMock } = require('./lx_mock');

async function load(filePath) {
  const lx = createLxMock();
  let capabilities;
  lx.on(lx.EVENT_NAMES.inited, (data, done) => {
    if (data && data.sources) capabilities = data.sources;
    done();
  });
  globalThis.lx = lx;
  delete require.cache[require.resolve(filePath)];
  require(filePath);
  await lx.send('inited', {});
  return {
    name: path.basename(filePath, '.js'),
    type: 'lx',
    _lx: lx,
    async search() { return []; },
    async getUrl(source, musicInfo, quality, exact = false) {
      const item = musicInfo.musicInfo || musicInfo;
      const origin = item.source || musicInfo.source || source;
      const tier = quality || '320k';
      if (exact && capabilities) {
        const supported = capabilities[origin];
        if (!supported || !supported.actions?.includes('musicUrl')) return null;
        if (origin !== 'local' && !supported.qualitys?.includes(tier)) return null;
      }
      const results = await lx.send('request', {
        action: 'musicUrl', source: exact ? origin : source,
        info: exact ? {type: origin === 'local' ? null : tier, musicInfo: item} : musicInfo,
        quality: tier,
      });
      const r = results && results[0];
      if (!r) return null;
      if (typeof r === 'string') return { url: r, quality };
      if (r.url) return { url: r.url, quality: r.quality || quality,
        ...(exact && typeof r.quality === 'string' ? {source_quality: r.quality} : {}) };
      return null;
    },
    async getLyric(source, musicInfo) {
      try {
        const results = await lx.send('request', {
          action: 'lyric',
          source: typeof musicInfo.source === 'string' && musicInfo.source || source,
          info: { ...musicInfo, musicInfo: musicInfo.musicInfo || musicInfo },
        });
        const r = results && results[0];
        if (typeof r === 'string') return { lyric: r };
        return r ? { lyric: r.lyric || '', lxlyric: r.lxlyric || null } : null;
      } catch (e) { return null; }
    },
  };
}

module.exports = { load };

// LX Music source loader.
const path = require('path');
const { createLxMock } = require('./lx_mock');

async function load(filePath) {
  const lx = createLxMock();
  globalThis.lx = lx;
  delete require.cache[require.resolve(filePath)];
  require(filePath);
  await lx.send('inited', {});
  return {
    name: path.basename(filePath, '.js'),
    type: 'lx',
    _lx: lx,
    async search() { return []; },
    async getUrl(source, musicInfo, quality) {
      const results = await lx.send('request', {
        action: 'musicUrl', source, info: musicInfo, quality: quality || '320k',
      });
      const r = results && results[0];
      if (!r) return null;
      if (typeof r === 'string') return { url: r, quality };
      if (r.url) return { url: r.url, quality: r.quality || quality };
      return null;
    },
    async getLyric(source, musicInfo) {
      try {
        const results = await lx.send('request', {
          action: 'lyric', source, info: musicInfo,
        });
        const r = results && results[0];
        return r ? { lyric: r.lyric || (typeof r === 'string' ? r : '') } : null;
      } catch (e) { return null; }
    },
  };
}

module.exports = { load };

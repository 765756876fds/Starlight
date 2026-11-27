"""Controlled MusicFree plugins and the production bridge for search tests."""
import os
import shutil

import pytest

from app.music.online.sources.js_source import JSMusicSource


@pytest.fixture
def paged_bridge(tmp_path, monkeypatch):
    node = os.environ.get("NODE_EXE") or shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the real MusicFree bridge")
    monkeypatch.setenv("NODE_EXE", node)
    monkeypatch.delenv("MF_USER_VARIABLES", raising=False)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    lx = tmp_path / "no-lx"
    lx.mkdir()
    (plugins / "fast.js").write_text("""
const { setTimeout } = require('timers/promises');
const failures = new Set();
module.exports = {
  platform: 'fast',
  async search(keyword, page, kind) {
    if (kind !== 'music') throw Error('unexpected search kind');
    if (keyword === 'split' || keyword.startsWith('old-')) await setTimeout(65);
    if (keyword === 'reload') await setTimeout(250);
    if (keyword === 'empty') return {data:[], isEnd:true};
    if (keyword === 'failure' && !failures.has(keyword)) {
      failures.add(keyword);
      throw Error('controlled failure');
    }
    const ids = {1: Array.from({length: keyword === 'split' ? 64 : 34}, (_, i) => i+1),
                 2: [1, keyword === 'split' ? 65 : 35], 3: [keyword === 'split' ? 66 : 36]};
    return {
      total: keyword === 'split' ? 66 : 36, isEnd: page === 3,
      data: ids[page].map(id => ({id, title: keyword + ' ' + id, artist: 'Artist',
                                album: 'Album', duration: 225, observedPage: page}))
    };
  }
};
""", encoding="utf8")
    (plugins / "slow.js").write_text("""
const { setTimeout } = require('timers/promises');
module.exports = {
  platform: 'slow',
  async search(keyword, page) {
    if (keyword === 'split') return new Promise(() => {});
    if (keyword === 'settles') await setTimeout(8200);
    if (keyword === 'failure') throw Error('controlled failure');
    if (keyword === 'empty') return {data:[]};
    return {isEnd:true, data:[{id:'1',title:keyword+' 1',artist:'Artist',album:'Album',
                              duration:225,observedPage:page}]};
  }
};
""", encoding="utf8")
    (plugins / "lookup.js").write_text("module.exports = {platform:'lookup'};", encoding="utf8")
    bridge = JSMusicSource(str(plugins), str(lx))
    try:
        assert bridge.available
        yield bridge
    finally:
        bridge.close()

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
    if (keyword === '星桥 纸鸢乐队') return {isEnd: true, data: [
      {id:"1",title:"星桥",artist:"纸鸢乐队",album:"夜航",duration:180,playCount:12,
        qualities:{standard:{sizeBytes:1048576,format:"mp3"}}},
      {id:"2",title:"回声",artist:"纸鸢乐队",album:"夜航",duration:218,playCount:120,
        sizeBytes:2097152,format:"mp3"},
      ...Array.from({length:27},(_,i)=>({id:"fill"+i,title:"曲目 "+i,artist:"独立乐队",duration:100}))
    ]};
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
    if (keyword === '星桥 纸鸢乐队') {
      const fs = require('fs');
      const marker = require('path').join(__dirname, 'release-b18');
      for (let i=0; i<500 && !fs.existsSync(marker); i++) await setTimeout(10);
      if (!fs.existsSync(marker)) throw Error('fixture release deadline');
      return {isEnd: true, data: [
        {id:"1",title:"星桥",artist:"纸鸢乐队",album:"夜航",duration:180,playCount:999,
          qualities:{super:{sizeBytes:16777216,format:"flac",sampleRate:96000,bitDepth:24}}},
        {id:"2",title:"星桥（Live）",artist:"纸鸢乐队",album:"现场",duration:194,
          playCount:1000,sizeBytes:31457280,format:"flac"}
      ]};
    }
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

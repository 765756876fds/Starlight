# Architecture

## JS 音源桥接架构

```
Python 主进程 ←→ (JSON-RPC over stdio) ←→ Node 桥接进程
                                              ↓
                                       MusicFree 音源（CommonJS）
                                       LX 音源（globalThis.lx 事件）
```

- 桥接脚本：`app/music/online/js_bridge/bridge.js`
- 协议：NDJSON（每行一个 JSON-RPC）
- 依赖：axios / cheerio / crypto-js / dayjs
- 音源目录：`data/music_sources/{musicfree,lx}/`

# LoL × 本地音乐 × 语音 Agent

英雄联盟信息助手 + 本地音乐助手，语音作为快捷操作方式。

## 核心原则

1. **不为了 AI 而 AI** — 能用确定性代码解决的，优先用代码
2. **AI 只解决"人话 → 意图"** — LLM 不直接控制电脑
3. **信息可以丰富，语音必须克制** — 监控详细，播报稀疏

## 功能模块

| 模块 | 状态 | 说明 |
|------|------|------|
| 🎵 本地音乐控制 | ✅ | mpv + JSON IPC，播放/暂停/下一首/音量 |
| 🎵 音乐模糊搜索 | ✅ | SQLite FTS5 + BM25 + 字符串相似度 |
| 🎵 音乐扫描入库 | ✅ | mutagen 读取标签，支持 FLAC/MP3/M4A |
| 🎮 2999 游戏状态 | ✅ | Live Client Data API，快照+状态差异 |
| 🎮 GameAlarm 计时 | ✅ | 闪现/技能/眼位/自定义计时器 |
| 🎮 LCU 客户端连接 | ✅ | 游戏流程、英雄选择、自动接受预留 |
| 🎙️ 语音识别 ASR | ⚙️ | Qwen3-ASR 本地模型，接口可替换 |
| 🧠 意图理解 Intent | ⚙️ | 规则匹配优先 + Qwen3 Function Calling |
| 🔊 TTS 语音输出 | ✅ | Edge TTS，后续可换 Kokoro |
| 🎯 Speech Judge | ✅ | S/A/B/C/DROP 等级，击杀聚合 |
| 👥 玩家数据分析 | 📋 | 预留架构 |
| 🖥️ UI 控制台 | 📋 | 预留架构 |

## 技术栈

- **语言**: Python 3.11
- **音频引擎**: mpv (JSON IPC / Named Pipe)
- **数据库**: SQLite + FTS5
- **ASR**: Qwen3-ASR (llama.cpp) / Faster-Whisper (备用)
- **Intent**: Qwen3-4B (llama.cpp Function Calling)
- **TTS**: Edge TTS
- **游戏数据**: Riot Live Client Data API (2999) + LCU
- **架构**: EventBus + CommandBus + StateStore + Scheduler

## 项目结构

```
lol-music-agent/
├── main.py                  # 主入口
├── 启动.bat                 # Windows 启动脚本
├── requirements.txt         # Python 依赖
├── config/
│   └── settings.yaml        # 全局配置
├── app/
│   ├── core/                # 核心基础设施
│   │   ├── config.py        # 配置加载
│   │   ├── event_bus.py     # 事件总线
│   │   ├── command_bus.py   # 命令总线
│   │   ├── state_store.py   # 状态存储
│   │   ├── scheduler.py     # 任务调度
│   │   └── logger.py        # 日志
│   ├── music/               # 音乐模块
│   │   ├── database.py      # SQLite + FTS5
│   │   ├── scanner.py       # 音乐扫描
│   │   ├── mpv.py           # mpv 控制
│   │   ├── queue.py         # 播放队列
│   │   ├── search.py        # 模糊搜索
│   │   └── controller.py    # 音乐控制器
│   ├── lol/                 # LoL 模块
│   │   ├── live_client.py   # 2999 API
│   │   ├── snapshot.py      # 游戏快照
│   │   ├── state_diff.py    # 状态差异
│   │   ├── events.py        # 事件定义
│   │   ├── game_alarm.py    # 游戏提醒引擎
│   │   ├── lcu.py           # LCU 连接
│   │   └── runtime.py       # LoL 运行时
│   ├── voice/               # 语音输入
│   │   ├── asr.py           # ASR 引擎接口
│   │   ├── intent.py        # 意图识别
│   │   └── runtime.py       # 语音运行时
│   ├── speech/              # 语音输出
│   │   ├── tts.py           # TTS 控制
│   │   ├── speech_judge.py  # 播报评判
│   │   └── kill_aggregator.py  # 击杀聚合
│   ├── player/              # 玩家数据（预留）
│   └── ui/                  # UI（预留）
├── data/                    # 数据目录
├── logs/                    # 日志目录
└── tests/                   # 测试
```

## 快速开始

### 1. 环境要求

- Windows 10/11
- Python 3.11+
- mpv 播放器
- 本地 GGUF 模型（Qwen3-ASR + Qwen3-4B）

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置

编辑 `config/settings.yaml`：
- 音乐扫描目录
- mpv 路径
- 本地模型路径
- ASR/Intent 参数

### 4. 运行

```bash
# 方式一：直接运行
python main.py

# 方式二：Windows 启动脚本
启动.bat
```

## 语音命令示例

### 音乐控制
- "下一首" / "换一首" / "切歌"
- "上一首"
- "暂停" / "暂停一下"
- "继续" / "继续播放"
- "声音大一点" / "音量小一点"
- "音量 50"
- "跳高潮" / "跳到副歌"
- "播放周杰伦的歌"
- "来一首类似现在这个的"

### 游戏提醒
- "对面莎弥拉闪现了，帮我记一下"
- "莎弥拉 W 用了"
- "这里插了个眼，帮我记一下"
- "两分钟后提醒我"

## 数据流

```
麦克风 → VAD → ASR → 文本 → 规则匹配? → 是 → 直接执行
                                      ↓ 否
                                    Qwen Intent → 结构化命令
                                                         ↓
                    2999 API → GameSnapshot → StateDiff → GameEvent → EventBus
                                                                          ↓
    mpv ← MusicController ← CommandBus ← Intent                GameAlarm
                                                                          ↓
                                                            SpeechJudge → TTS
```

## 开发原则

1. **先研究再实现** — 任何功能开发前先检索成熟项目和官方文档
2. **确定性优先** — 能用代码解决的不上 AI
3. **Read > Infer** — 游戏能直接读取的数据，不自己计算
4. **安全第一** — 不做外挂/脚本/自动玩游戏，不读取游戏内存
5. **模块化设计** — EventBus 解耦，模块可独立替换

## 明确不做

- ❌ 自动控制英雄移动/技能/攻击
- ❌ 游戏内存读取 / DLL 注入
- ❌ 视觉识别作为核心数据源
- ❌ 聊天机器人式人格陪聊
- ❌ 复杂多 Agent / RAG（第一版）

## 许可证

个人项目，仅供学习使用。

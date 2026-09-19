# Core Runtime V1.x — Baseline

> 本文件是 V1.x 的冻结基线说明。只记录真实生产路径与产品边界，不罗列“规划中”的功能。
> 基线 commit：见 `git log -1`；tag：`v1.x-core-runtime`。

## 1. Product Definition
本项目是一个**以语音快速控制为核心的 LoL 当前对局信息 + 本地音乐助手**。

它做的事：
- 读取当前对局中**可观测**的游戏状态（2999 Live Client Data）。
- 回答少数“少看一次记分板”的确定性问题（经济差 / 谁最肥 / 我的 KDA）。
- 在关键刷新时间主动提醒（巢虫 / 小龙 / 大龙 / 远古）。
- 记录**用户主动告诉它**的临时信息（如“对面莎弥拉闪现了”），并到点提醒。
- 用语音控制本地音乐（mpv）。

它**不**做的事：
- 自动战斗 / 视觉 Bot / 自动鼠标键盘。
- 读取内存、DLL 注入、修改游戏文件。
- 推断敌方隐藏信息、自动追踪敌方技能。
- AI 教练 / “你应该怎么打”。
- RAG / 向量数据库 / Embedding。
- 跨局战绩的智能分析产品化（SGP 作为基础设施保留）。
- Faster-Whisper 作为生产 ASR。

## 2. Production Call Chains

### Voice
麦克风 → RMS 阈值监听 → Qwen3-ASR（llama-server :8080）→ `RuleBasedIntent`
→ 规则命中直接返回；未命中才进 LLM fallback（白名单 + 安全门）→ CommandBus → Handler。

### Music
语音 Intent（`music.*`）→ CommandBus → MusicController → mpv（named-pipe IPC）。
搜索走 SQLite + FTS5 BM25。

### LoL 2999
`runtime._poll_game_data` → `SnapshotBuilder.from_api_data` → `GameSnapshot`
→ `StateDiff.diff` → EventBus（GAME_TIME_UPDATE / CHAMPION_KILL / BUILDING_KILL / OBJECTIVE 等）。

### Reminder / GameAlarm
游戏时间推进 → `GameAlarmEngine._on_game_time_update` → `_check_alarms`
→ ALARM_TRIGGERED → SpeechJudge → SpeechQueue → TTS。
ObjectiveTimer 在真实 `game_time>0` 到达后才排首次刷新（重启附着不补播历史刷新）。

### User Memory
用户命令 → `alarm.flash / spell / ward / custom` → main.py 创建 Alarm（`data["source"]=USER_MEMORY`）
→ `_say`（SPEECH_SAY high）直连 TTS 快速确认；到点走 Reminder 链。

### Speech
普通事件 → SpeechJudge 定级（S/A/B/C/DROP）→ SpeechQueue（堆，MAX=10，TTL，event_id 去重，priority，可打断）→ TTSController。

### ActionFeedback（特殊快速路径）
用户主动操作结果 → SPEECH_SAY(high) → TTSController.speak **直连**，绕开普通 SpeechQueue，可打断当前播放。

## 3. External Services (must be started separately)
Agent **不负责**拉起这些服务：
- ASR：Qwen3-ASR-0.6B llama-server，`http://127.0.0.1:8080/v1`。
- Intent LLM：Qwen3-4B，LM Studio OpenAI 兼容 `http://127.0.0.1:1234/v1`。
- LoL：游戏客户端自带 2999（Live Client Data）与 LCU。
- 本地播放：mpv。

启动：`启动.bat`（开发）或 `启动_无窗口.vbs`（pythonw）。Agent 会在 ASR 未就绪时重试连接。

## 4. Frozen Boundary
- **CORE**：语音输入(Qwen3-ASR)、Rule-first Intent + 安全 fallback、本地音乐(mpv+FTS5)、2999 Snapshot/StateDiff/EventBus、GameAlarmEngine、ObjectiveTimer、TemporaryGameMemory(flash/spell/ward/custom)、SpeechJudge/Queue/TTS、ActionFeedback 直连。
- **INFRASTRUCTURE**：SGP 战绩线（数据/UI）、LCU 身份与 gameflow、DataDragon 静态数据、UI 面板。
- **FROZEN**：SGP 历史产品化、Highlight、AI 教练、自动敌方技能追踪。
- **REJECTED**：自动战斗 / 视觉 Bot / 内存读取 / DLL / 游戏文件修改 / RAG / 向量库 / Faster-Whisper 生产备用。

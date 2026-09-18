# PHASE A0 — Project Archaeology & Cleanup Audit

> 本报告为**只读考古审计**结论。审计对象为**当前工作区（含未提交修改）**，不是干净的 HEAD。
> 本阶段未修改任何业务代码，仅新增本文件 `docs/A0_PROJECT_AUDIT.md`。
> 状态词定义：`NOT_STARTED / SKELETON / IMPLEMENTED / INTEGRATED / REAL_TESTED / DONE`。

---

## 1. Git 状态

- **branch**: `master`
- **HEAD（实际）**: `fded3a6 fix(music): mpv cleanup, js source alias, status bar live update, dark theme`
- **与任务书基线的偏差（重要）**：任务书称基线为 `a1c87c7 feat: complete player analysis and query foundation`。实际 HEAD 已是 `fded3a6`，**领先 `a1c87c7` 共 10 个提交**，且领先 `origin/master` 10 个提交（未推送）。这 10 个提交已包含：automation（queue/accept/ban/pick）、cooldown 提醒、UI 控制台、overlay、ongoing 对局信息、online music 框架。
- **git status（摘要）**:
  - 20 个已跟踪文件被修改，**+709 / −150**，全部未提交。重点：`main.py`（+126/−… 重写）、`app/ui/panels/*`、`app/music/controller.py`、`app/lol/automation/service.py`、`app/player/query_handler.py`、`app/voice/asr.py`、`启动.bat`。
  - 大量**未跟踪**文件：根目录 `audit_*.py` / 根目录 `test_*.py`、`tools/`（探针/脚本）、`viz/`、`data/*.jsonl`、`data/test_music.db`、`data/tts*.mp3`、`pytest_out.txt`、`docs/项目工作报告.md` 等。
- **结论**：工作区处于“开发中、未提交”状态。本报告反映的是这个工作区，不是可复现的某个 commit。**任何清理动作前都应先由项目负责人决定是否先 commit/归档这些未提交工作。**

---

## 2. 项目入口

- **启动方式**:
  - `python main.py`（开发）
  - `启动.bat`（Windows；本工作区中该文件已被修改，行数大幅缩减）
  - 可选参数 `--headless`（无 UI，纯后台）
- **实际入口**: `main.py` → `main()` → `Application` 单例 → `Application.run()`
- **实际初始化链**（`Application.initialize()`，有 `_initialized` 防重入守卫）：
  ```
  Config(单例, config/settings.yaml)
   └─ EventLog                                  # logs/events/*.jsonl，敏感字段脱敏
   └─ MusicController + OnlineSourceManager     # 本地音乐 + 在线源
   └─ LoLRuntime(config_data)                   # 2999 + LCU + 快照 + 差分 + 告警 + 对局
   └─ TTSController                             # edge-tts → mpv 子进程
   └─ SpeechQueue → SpeechJudge(queue=…)        # 优先级播报队列 + 游戏事件分级
   └─ VoiceRuntime                              # 麦克风 VAD → ASR → 意图 → 命令
   └─ _register_alarm_events   (EventBus 订阅 alarm.* → GameAlarmEngine)
   └─ _register_player_query   (CommandBus 注册 PLAYER_QUERY → query_handler)
   └─ _register_automation     (LoLAutomationService + AUTO_ACCEPT_TOGGLE/AUTO_BANPICK_TOGGLE)
   └─ _register_cooldown        (CooldownManager + COOLDOWN_* handlers)
   └─ _register_ongoing         (OngoingGameService.start, ChampSelect 时加载 10 人快照)
  ```
- **start() 顺序**：`event_log → scheduler → music(+scan 线程) → lol → tts → speech_queue → speech_judge → voice`。
- **UI 模式**：先创建空壳 `MainWindow`（loading），后台线程跑 `initialize()+start()`，完成后 `_switch_to_tabs()` 换成 `MainTabs`（真正面板）。
- **headless 模式**：`initialize()` 后直接 `start()`，进入阻塞循环，不建 Qt。

---

## 3. 当前架构图（真实调用链）

```
                          main.py (Application)
                                │
        ┌───────────────┬───────┴────────┬───────────────┬──────────────┐
        │               │                │               │              │
   EventBus        CommandBus       StateStore        Scheduler        Config
   (core/)         (core/)          (core/)           (core/)        (settings.yaml)
        ▲               │                ▲                ▲
        │               ▼                │                │
        │        ┌──────┴───────┐       │                │
        │        │              │       │                │
   game事件   MusicController  VoiceRuntime(Intent)   CooldownManager / GameAlarmEngine
        │        (music/)       (voice/)               (lol/cooldown, lol/game_alarm)
        │            │              │
        │            ▼              ▼
        │        mpv IPC      CommandBus(music.*,player.query) / EventBus(alarm.*)
        │        (music/mpv)
        │
   LoLRuntime (lol/runtime.py)
     ├─ _poll_game_data (≈1s)
     │     LiveClientAPI(2999) → SnapshotBuilder → PlayerAnalysisService
     │     → StateDiff.diff → EventBus.publish(GameEvent)
     ├─ _poll_lcu (≈1s)
     │     LCUConnection → gameflow_phase / current_summoner / champ_select
     │     → StateStore + EventBus
     ├─ GameAlarmEngine / ObjectiveTimer (start)
     └─ PlayerRepository / PlayerAnalysisService / MatchRepository (持久化)
        │
        ▼
   SpeechJudge (订阅 game 事件) → SpeechQueue(优先级/去重/TTL/打断)
        → TTSController(edge-tts → mpv 子进程)

   LoLAutomationService (lol/automation/, 默认关闭)
     worker 0.5s 轮询 gameflow_phase
       ├─ QueueController.try_start      (POST /lol-lobby/.../search)
       ├─ ReadyCheckController.check     (4 步 → POST /lol-lobby/.../accept)
       └─ ChampSelectController.poll     → ChampSelectActionExecutor.two_phase_patch (PATCH hover→lock)
   OngoingGameService (独立轮询 ChampSelect → OngoingGameLoader → 10 人快照 → UI)
```

**关键事实**：任务书预期的整条链路（用户启动→入口→Core→LCU→2999→EventBus→CommandBus→Voice→Music→Speech→Player）**都真实存在**，不是空架子。

---

## 4. LCU

- **文件**: `app/lol/lcu.py`
- **类**: `LCUConnection`（全项目唯一 LCU client，无第二实现）
- **关键函数**:
  - `_discover_connection()`：双通道——lockfile 解析 **或** `psutil` 读游戏进程命令行（含国服 `LeagueClient.exe`/`client.exe`，支持 `---app-port/---remoting-auth-token`）。
  - `request()/request_raw()`：HTTP，HTTPBasicAuth，`requests.Session`；`_request_lock` 串行化（线程安全，对应提交 d5eca5e）。
  - 业务方法：`get_gameflow_phase()`、`get_current_summoner()`、`get_champ_select_session()`、`accept_matchmaking()`、通用 `get/post/put/patch/delete`。
  - WebSocket **默认关闭**（`_use_websocket=False`），全部走 HTTP 轮询。
- **调用者**:
  - `app/lol/runtime.py`（gameflow/summoner/champ_select 轮询、对局结果）
  - `app/lol/automation/*`（queue/ready/champ_select/action_executor，发写请求）
  - `app/lol/ongoing/service.py`（ChampSelect 会话 + 选手近期对局）
  - `app/player/service.py`（经 `analyze_game` 读 self puuid）
- **真实调用**: ✅ 是。discovery/锁文件/国服进程解析均有单测；真实连接测试在测试中 `SKIPPED`。
- **状态**: `IMPLEMENTED`（代码完整，含线程锁、错误容忍）；本审计无运行中客户端，**未做 REAL_TESTED**。

---

## 5. 2999 (Live Client Data API)

- **文件**: `app/lol/live_client.py`
- **类**: `LiveClientAPI`
- **端点**: `/allgamedata`、`/activeplayer`、`/playerlist`、`/eventdata`、`/gamestats`、`/activeplayerabilities` 等（含国服 `/liveclientdata/` 前缀变体，`is_available` 自动探测）。
- **下游**:
  ```
  LiveClientAPI.get_all_game_data()
    → SnapshotBuilder.from_api_data()            # app/lol/snapshot.py
      → GameSnapshot (players/teams/objectives/...)
    → StateDiff.diff(prev, cur, events)          # app/lol/state_diff.py
      → List[GameEvent] (kill/multikill/object/item/spell/levelup/game_end)
    → LoLRuntime 映射为 core Event → EventBus.publish
  ```
- **真实调用**: ✅ 是。`LoLRuntime._poll_game_data` 每秒调用；`eventdata` 还被用于 GameEnd 结果缓存（`_cache_game_end_from_eventdata`）。
- **状态**: `IMPLEMENTED`。真实对局管道在测试中 `SKIPPED`（需运行中对局），但有“腾讯真实样本 108”离线回放测试覆盖映射。

---

## 6. EventBus

- **定义**: `app/core/event_bus.py`，单例 `get_event_bus()`。
- **形态**: 同步 `publish(event)`；`Event(event_type: str, data: dict, timestamp, priority, source)`；常量集中在 `EventType`。
- **producer**: LoLRuntime（游戏/lcu 事件）、VoiceRuntime（`voice.command_executed`）、TTSController（`tts.*`）、CooldownManager（`cooldown.*`）、automation（`automation.*`）、SpeechJudge。
- **consumer**: EventLog、SpeechJudge、GameAlarmEngine、ObjectiveTimer、main.py 的 `_register_*`、UI（经 Qt 桥）、TTSController（订阅 `speech.say`）。
- **关键确认**: `2999 → SnapshotBuilder → StateDiff → EventBus` **真实存在**（见第 3、5 节）。
- **状态**: `IMPLEMENTED`，核心稳定。

---

## 7. CommandBus

- **定义**: `app/core/command_bus.py`，单例。`register(type, handler)` / `execute(cmd)` / `execute_async(cmd)`；`Command(command_type, params, source)`；常量 `CommandType`。
- **producer（谁产生 Command）**:
  - `VoiceRuntime._execute_intent()`（音乐 `music.*`、`player.query`）。
  - UI 面板（automation/cooldown/music 按钮）。
  - 自身注册的 toggle 命令。
- **consumer（谁消费）**:
  - `MusicController._register_commands`（music.* 全部）。
  - `CooldownManager.handle_start/cancel/cancel_all/list`。
  - `LoLAutomationService.toggle_auto_accept/toggle_auto_banpick`。
  - `make_player_query_handler`（player.query）。
- **重点链路**: Voice → Intent → `command_bus.execute` → MusicController ✅ 真实。
- **LoL Automation 预留**: 已存在 `automation.*` 相关 CommandType/事件，且 LoLAutomationService 已接 toggle 命令；但**自动 queue/ban/pick 不是由命令触发，而是由内部 worker 轮询 phase 驱动**。
- **状态**: `IMPLEMENTED`。

---

## 8. Voice / ASR

- **文件**: `app/voice/runtime.py`、`asr.py`、`intent.py`、`ptt.py`
- **实际链路**:
  ```
  麦克风(pyaudio) → _mic_loop(内联 RMS 音量阈值 VAD)
    → ASRManager.transcribe(wav)
      主: Qwen3ServerASR (HTTP llama-server :8080)
      备: Qwen3ASR (gguf 本地)
      备: FasterWhisperASR (始终挂链，主备失败才触发，懒加载)
    → IntentRouter.parse(text)
      规则优先(RuleBasedIntent) → llama-cpp(Qwen3-4B) → OpenAI API(可关 think)
    → VoiceRuntime._execute_intent(intent)
      → CommandBus.execute(music.*/player.query) 或 EventBus.publish(alarm.*)
  ```
- **哪些是生产路径**: 规则 → 命令/事件；Qwen3Server ASR。
- **Faster-Whisper**: `app/voice/asr.py` 中 `FasterWhisperASR` 类存在，被 `ASRManager.initialize` **实例化并加入引擎列表**（fallback），**不是死代码**——仅在主引擎失败时才加载。按要求**不删，只报告**。
- **PTT (`app/voice/ptt.py`)**: ❌ **无任何外部 import**。实际 VAD 是 runtime 内联的 RMS 阈值，`asr.ptt_key` 配置无人读取。`ptt.py` 整体未接线。
- **已知缺口（重要）**: 意图规则能产出 `cooldown.start / cooldown.cancel / cooldown.cancel_all`，CommandBus 也注册了对应 handler，但 **`_execute_intent()` 没有处理 cooldown.* 的分支** → 语音说“对面闪现了”只会被规则识别成 Intent，却**不会**执行（仅发 `voice.command_executed`）。语音 cooldown 桥接缺失，当前只能由 UI 触发。
- **状态**: Voice/ASR `IMPLEMENTED`；PTT `NOT_STARTED`（死文件）；语音→cooldown `SKELETON`。

---

## 9. Music

- **文件**: `app/music/`：`controller.py`、`database.py`(SQLite+FTS5/BM25)、`scanner.py`(mutagen)、`search.py`、`queue.py`、`mpv.py`(JSON IPC + pipe 锁)、`online/`。
- **真实链路**: `CommandBus(music.*)` → `MusicController` → MusicDB / Search / PlayQueue / MpvController → mpv IPC。
- **music_highlight（重点）**: **`IMPLEMENTED`**——DB 有 highlight 表（confidence/song_id/start/end）、`jump_to_highlight`（单次跳）、`set_highlight_mode`（段播轮询线程，到点 next）、`mark_highlight_start`（手动标）。有对应单测（`test_highlight.py`、`test_playback_mode.py` 的 highlight 段）。不是 skeleton。
- **Online（Module 5）**: `OnlineSourceManager`（注册/并发 search 线程池 + 缓存 + load_custom_sources 热加载）。源：`demo_source`（**返回空，stub**）、`archive_source`（真实 HTTP）、`js_source`（经 `js_bridge/bridge.js` NDJSON 调 Node 侧 MusicFree/LX 音源，依赖 node + `data/music_sources/`）。
- **状态**: 本地音乐 `INTEGRATED`；在线音乐框架 `IMPLEMENTED`，但源为“1 个真实(archive) + 1 个 stub(demo) + 1 个需 node/数据(js)”，整体**部分可用**。

---

## 10. Speech / TTS

- **文件**: `app/speech/`：`tts.py`、`speech_judge.py`、`speech_queue.py`、`rule_engine.py`。
- **真实链路（已确认）**:
  ```
  EventBus(game 事件) → SpeechJudge(订阅)
     → 分级(S/A/B/DROP) + 频控 + 文本生成
     → SpeechQueue.enqueue(SpeechItem)   # main.py 构造时把 queue 注入 judge
     → worker 线程(优先级堆 / 按 event_id 去重 / TTL / 可打断)
     → TTSController.speak(text)         # edge-tts → mp3 → mpv 子进程播放
  ```
  另：TTSController 也直接订阅 `speech.say`（无 queue 的直通路径）；cooldown/player 查询走直接 `speech_queue.enqueue`。
- **设计要点**: 多数击杀/推塔类事件被 SpeechJudge 设为 `DROP`（“信息丰富、语音克制”），仅 S(超神/团灭) 与 B(告警) 播报。
- **`kill_aggregator.py`**: **已删除**，仅在注释/旧 README 中残留提及。
- **状态**: `INTEGRATED`，有完整生命周期/打断/TTL 单测。

---

## 11. Player

- **文件**: `app/player/`：`repository.py`、`service.py`、`match_repository.py`、`history_repository.py`、`profile_service.py`、`game_analysis.py`、`event_log_reader.py`、`query_service.py`、`query_handler.py`、`response_formatter.py`、`models.py`。
- **两套并存（分层，非重复类）**:
  1. **运行时基础层**（LoLRuntime 用）：`PlayerRepository`（写 `players` 身份表）+ `PlayerAnalysisService`（从 2999 snapshot 构建当前对局 ally/enemy，阵容签名缓存，仅持久化 self puuid）。`MatchRepository` 写 `match_records/match_players`（对局结束落库）。
  2. **语音查询层**（main.py 用）：`HistoryRepository`（只读比赛表）+ `PlayerProfileService` + `GameAnalysisService` + `PlayerQueryService` + `make_player_query_handler`（`player.query` → 格式化 → 入 SpeechQueue 播报）。
- **数据库**: 都连 `data/player.db`，但各自 `sqlite3.connect`，**无共享连接池/无统一 schema 管理**（`players` / `match_records` / `match_players` 多表）。
- **哪些真用**: 运行时层被 `LoLRuntime` 用；查询层被 main.py 注册到 CommandBus 用。均非 skeleton。
- **旧 vs 新**: 不存在“旧 Player 系统可删”——基础层（in-game 实时）和查询层（语音历史）职责不同。重复风险在于“两个 generation 各自建表/连库”，而非同名双类。
- **状态**: `IMPLEMENTED`；建议 `REFACTOR LATER`（统一 DB 访问与 schema）。

---

## 12. Automation（本次重点）

- **真实执行路径**: `LoLAutomationService`（`app/lol/automation/service.py`）worker 线程每 0.5s 读 gameflow phase，**直接调用 controller**：
  - `QueueController.try_start` → `POST /lol-lobby/v2/lobby/matchmaking/search`
  - `ReadyCheckController.check` → 探测 `searching/ready_check/in_progress/canceled` 四态 → `POST /lol-lobby/v2/ready-check/accept`
  - `ChampSelectController.poll` → 找我的进行中动作 → `ChampSelectActionExecutor.two_phase_patch`（先 PATCH `championId` hover → 可中断等待 0.15s → PATCH `completed:true`）
- **逐功能状态**：
  - **Auto Queue = `IMPLEMENTED`**（真发 LCU 请求）。默认关；触发靠 `automation.auto_queue` 或 UI toggle。
  - **Auto Accept = `IMPLEMENTED`**（4 步 ready check + accept）。默认关。
  - **Auto Ban = `IMPLEMENTED`**（两阶段 PATCH），**但 `presets.ban_priority` 在 settings.yaml 为空** → 即使开启也找不到候选英雄。
  - **Auto Pick = `IMPLEMENTED`**（两阶段 PATCH），同上 `pick_priority` 为空。
  - **Safe Stop = `IMPLEMENTED`**（`stop_event`，two-phase 之间可被 `stop()` 打断，状态置 `SAFE_STOP`）。
- **重大发现——并行死抽象**: `self._features`（`features/auto_queue.py`、`auto_accept.py`、`auto_ban.py`、`auto_pick.py`、`base.py`、`fallback.py`、`verification.py`、`safe_stop.py`）每个又**内部 new 了自己的 controller**，但全代码库**没有任何地方调用其 `.tick()`**（grep 确认）。service 只在 `feature_status()`/`_sync_feature_switches` 里读它的 `.enabled`。**features 层是未接线的骨架，真实走的是 service 自己持有的 controller。**
- **配置现状**: `automation.enabled: false`，四个开关默认 false，`polling_interval: 0.5`，`presets` 优先级列表为空。
- **本阶段结论**: 四个自动动作**代码上已实现且真发请求**，但“默认关闭 + 无英雄优先级配置 + 本审计无运行客户端未真测” → 标 `IMPLEMENTED`（非 REAL_TESTED/DONE）；features 层标 `SKELETON`。

---

## 13. Config

- **加载器**: `app/core/config.py`，单例，读 `config/settings.yaml`；`get(dotted, default)`、`get_section(name)`。**只有一个 loader**。
- **当前结构**: `music / mpv / voice(asr,tts,intent) / voice_command / alarm / game_event / automation`（以及根目录旧 `auto:` 段）。
- **重复/旧/未使用（重点）**:
  - **存在两段 automation 配置**：
    - 旧 `auto:`（`auto_accept.enabled`、`auto_ban_pick.enabled`、`presets`）——**grep 无任何代码读取**，死配置。
    - 新 `automation:`（`enabled`/`auto_queue`/`auto_accept`/`auto_ban`/`auto_pick`/`polling_interval`/`presets`）——**main.py 实际读这个**。
  - `asr.ptt_key`：无代码读取（PTT 未接线）。
- **状态**: `IMPLEMENTED`；建议清理旧 `auto:` 段与 `ptt_key`。

---

## 14. Tests

- **归档结果（`pytest_out.txt`，此前运行）**: collected **321**，**314 passed / 7 skipped**，~28s。skipped 全部是“需要运行中客户端”的真实 LCU/2999 连接测试。
- **本审计复核**: 当前系统 Python（3.11）**未安装 pytest**（`No module named pytest`），沙箱 python 也无 pytest。按只读原则**未擅自 pip install**，因此**未能在本次重新执行**。以上为最近一次归档结果，可能滞后于新增的 player/automation/online/overlay 测试文件。
- **覆盖范围（看测试名+断言）**:
  - core：EventType 别名、EventBus、StateDiff、GameAlarm、EventLog 脱敏与落盘、EventReplay。
  - 对局事件：多杀单源、riot_event_id 去重、腾讯 liveclientdata 前缀、GameEnd 结果缓存、核心 11 类事件 E2E、真实腾讯样本 108 映射。
  - LCU：discovery/锁文件/进程命令行/日志不含 token；champ_select 状态归一化与去抖；gameflow phase；summoner。
  - Music：DB/FTS/增量扫描、playback mode、highlight 数据库与段播轮询、mpv pipe 线程锁。
  - Speech/TTS：rule_engine 候选、SpeechJudge→queue 分级/频控/去重、SpeechQueue 优先级/打断/TTL/溢出、TTS 生命周期与 generation 防护。
  - Intent：规则、OpenAI API、标点归一化、voice dispatch。
  - 新测试（player_query、automation、cooldown、online、overlay、ongoing 等）在 tests/ 下存在，但**是否被上述归档 run 收集到无法确认**（归档 list 未列出它们）。
- **测试性质**: 几乎全是 **mock/单元 + 离线样本回放**；真实 LCU/2999/对局集成测试为 SKIPPED。

---

## 15. Duplicate（重复/重叠实现）

| # | A | B | 区别 | 谁在用 | 建议 |
|---|---|---|---|---|---|
| D1 | `automation/features/*`（各 feature 内 new 自己的 controller） | `automation/service.py` 持有的 `Queue/ReadyCheck/ChampSelectController` | features 有 `.tick()` 抽象但**从不被调用**；controller 是真实执行路径 | controller | 保留 controller，features 层待删/重构 |
| D2 | settings.yaml 旧 `auto:` 段 | settings.yaml 新 `automation:` 段 | 旧段无代码读取；新段被 main.py 读 | 新 `automation:` | 删旧段 |
| D3 | `main.py` 中 `from app.lol.cooldown import CooldownManager` | `from app.lol.cooldown.manager import CooldownManager as _CM` | 同一类引两次，前者未用 | 后者 `_CM` | 删重复 import |
| D4 | `GameAlarmEngine`（voice `alarm.*` 触发，闪现/技能/眼/自定义计时） | `CooldownManager`（CommandBus/UI 触发，技能/召唤师技能冷却） | **两套并行“计时提醒”系统**，功能重叠（都记闪现/技能 CD） | 两者都在跑 | 长期合并为一 |
| D5 | `LoLRuntime._poll_champ_select`（写 StateStore） | `OngoingGameService._tick`（再次 GET `/lol-champ-select/v1/session`） | 对同一 ChampSelect 会话**重复轮询**两个消费者 | 两者都在跑 | 长期共用一次抓取 |
| D6 | Player：`PlayerRepository`(身份表) vs `Match/HistoryRepository`(比赛表) | 同库不同表，各自 `sqlite3.connect` | 非同名双类，但无统一 DB 层 | 都在用 | 统一 DB 访问（REFACTOR） |

---

## 16. Deprecated（疑似废弃，附证据）

| # | 项 | 证据 |
|---|---|---|
| P1 | `app/voice/ptt.py` | 全代码库无 `import ptt` / `from .ptt`；真实 VAD 在 runtime 内联 RMS |
| P2 | settings.yaml 旧 `auto:` 段 + `asr.ptt_key` | 无 `config.get("auto.*")` 读取；ptt_key 无引用 |
| P3 | `app/speech/kill_aggregator.py` | 文件已不存在，仅注释/README 残留（已删，仅记录） |
| P4 | README.md | 仍写“玩家数据/UI 预留”、引用已删的 kill_aggregator，与代码现状不符 |
| P5 | 根目录散落脚本：`audit_*.py`、根 `test_*.py`（`test_2999_*`、`test_core.py`、`test_music.py`、`test_lmstudio_api.py` 等）、`check_fixture.py`、`fix_fixture.py`、`list_mics.py` | 与 `tests/` 正式测试重名/重叠，均为一次性探针；不在 pytest 正式收集路径 |
| P6 | `tools/` 下大量 `probe_*/test_*/check_*` 脚本与 `*.json` 样本、`viz/` | 开发期探针/可视化，非产品代码 |
| P7 | 数据/产物杂物：`data/test_music.db`、`data/tts*.mp3`、`pytest_out.txt`、`data/*.jsonl` 日志 | 非源码 |

> 说明：Faster-Whisper（`asr.py`）**不是**废弃——它是 ASR fallback 链的真实一环。

---

## 17. Skeleton / “假完成”

| # | 项 | 证据 |
|---|---|---|
| S1 | `automation/features/*` | 类/方法齐全、被实例化并 toggle `.enabled`，但 `.tick()` 零调用，从不执行 |
| S2 | 语音 → CooldownManager | 规则产出 `cooldown.*` Intent、CommandBus 已注册 handler，但 `VoiceRuntime._execute_intent` 无对应分支，语音不触发 |
| S3 | 在线音乐 `demo_source` | `search()` 直接返回空（`not implemented`） |
| S4 | Automation ban/pick 配置 | 两阶段 PATCH 代码完整，但 `presets.ban_priority/pick_priority` 为空，开启也无英雄可选 |
| S5 | 自动化“真测” | 代码真发 LCU 请求，但默认全关、无真实对局/选人实测记录，状态停在 `IMPLEMENTED` 而非 REAL_TESTED |

---

## 18. Final Requirement Gap

| Module | 最终需求 | 当前实现 | 状态 | 代码位置 | 缺口 |
|---|---|---|---|---|---|
| M1 | LoL Client Automation：Auto Queue/Accept/Ban/Pick/Safe Stop/Config | service+controllers 全部真发 LCU | `IMPLEMENTED`（未真测/默认关/无优先级配置） | `app/lol/automation/` | features 死层、presets 空、缺真实对局验证；Safe Stop 已有 |
| M2 | Manual Cooldown Reminder | CooldownManager+数据目录+TTS+CommandBus+UI | `IMPLEMENTED`（后端） | `app/lol/cooldown/` | **语音桥接缺失（S2）**；与 GameAlarmEngine 功能重叠（D4） |
| M3 | Voice | mic→RMS VAD→ASR(主+备)→Intent(规则/LLM)→命令 | `IMPLEMENTED` | `app/voice/` | PTT 死代码；cooldown 语音未接 |
| M4 | Local Music | FTS5/scan/mpv/queue/highlight | `INTEGRATED` | `app/music/` | 无重大缺口 |
| M5 | Online Music | 框架+archive/js 源 | `IMPLEMENTED`（部分） | `app/music/online/` | demo 是 stub；js 源依赖 node+音源数据；未端到端验证 |
| M6 | UI Control Center | main_window/tabs/panels/tray/statusbar | `IMPLEMENTED` | `app/ui/` | 本审计无 GUI 环境未目视验证；工作区有大量未提交 UI 改动 |
| M7 | Overlay | overlay_window/panel/model | `IMPLEMENTED`（代码层） | `app/ui/overlay/` | 未在真实对局目视验证 |
| M8 | Player/teammate/enemy info | in-game 分析 + 语音历史查询 + ChampSelect 10 人快照 | `IMPLEMENTED` | `app/player/`, `app/lol/ongoing/` | DB 访问分散；ongoing 与 runtime 重复轮询 champ-select（D5） |

---

## 19. KEEP（必须保留）

- `app/core/`：`event_bus.py`、`command_bus.py`、`state_store.py`、`scheduler.py`、`config.py`（核心骨架，稳定）。
- `app/lol/lcu.py`、`live_client.py`、`snapshot.py`、`state_diff.py`、`runtime.py`、`events.py`、`game_alarm.py`、`objective_timer.py`。
- `app/music/`：`controller/database/mpv/queue/search/scanner`（本地音乐主链路）。
- `app/speech/`：`tts.py`、`speech_judge.py`、`speech_queue.py`、`rule_engine.py`。
- `app/voice/`：`runtime.py`、`asr.py`、`intent.py`（主链路；含 Faster-Whisper fallback）。
- `app/lol/cooldown/manager.py` + `app/lol/data/*`（英雄/技能/召唤师技能目录）。
- `app/lol/automation/`：`service.py`、`queue_controller.py`、`ready_check_controller.py`、`champ_select_controller.py`、`action_executor.py`、`models.py`、`state_machine.py`。
- `app/lol/ongoing/`（对局内玩家信息）。
- `tests/` 正式测试目录、`requirements.txt`、`config/settings.yaml`（清理后）。

## 20. REFACTOR LATER（现可用，待整理）

- Player 层（统一 DB 连接/schema；合并运行时层与查询层的数据访问）。
- `app/music/online/`：源抽象与错误处理；js_bridge 的 node 依赖管理。
- UI（工作区有大量未提交改动，先稳定提交再谈结构）。
- `LoLRuntime` 与 `OngoingGameService` 对 ChampSelect 的重复轮询（D5）。
- `main.py` 的初始化/start 段较长，可拆 module，但**本阶段不动**。

## 21. REPLACE（与最终架构冲突，未来替换）

- `automation/features/*` 整套抽象（S1/D1）：真实路径是 service 直接持有的 controller；features 层要么删掉、要么改造为真正的可插拔策略，二选一，不要维持现状。
- 旧 `auto:` 配置段（D2）→ 由 `automation:` 段取代。
- `GameAlarmEngine` vs `CooldownManager`（D4）：长期合并成一套“技能/召唤师技能冷却提醒”，避免两套计时并行。

## 22. DELETE AFTER APPROVAL（建议删除，本阶段未删）

- `app/voice/ptt.py`（P1，无引用）。
- settings.yaml 旧 `auto:` 段与 `asr.ptt_key`（P2/D2）。
- `main.py` 重复的 `from app.lol.cooldown import CooldownManager`（D3）。
- 根目录一次性探针：`audit_*.py`、根 `test_*.py`、`check_fixture.py`、`fix_fixture.py`、`list_mics.py`（P5，确认无保留价值后归档/删除）。
- `tools/` 下纯一次性 probe/test 脚本、`viz/`、`data/test_music.db`、`data/tts*.mp3`、`pytest_out.txt`、`data/*.jsonl` 旧日志（P6/P7）。
- 过时 README（P4）→ 重写而非直接删。

## 23. DO NOT TOUCH（稳定、勿为美观而动）

- `app/core/event_bus.py`、`app/core/command_bus.py`、`app/core/state_store.py`、`app/core/scheduler.py`。
- `app/lol/state_diff.py`（事件去重/多杀单源逻辑，有大量回归测试）。
- `app/speech/speech_queue.py`（优先级/打断/TTL 防护）、`speech_judge.py` 的分级表。
- `app/lol/lcu.py` 的请求锁与发现逻辑。
- `app/music/mpv.py` 的 pipe 串行锁。
- 任何对 Voice/ASR/Music/TTS/EventBus/CommandBus/Player 主链路的“顺手重构”——本阶段明确禁止。

---

## 24. GitHub Migration Plan（仅评估，不移植）

> 说明：以下基于对这两个成熟仓库公开结构的认知做定性对照；本阶段未 clone、未移植任何代码。

- **LeagueAkari（https://github.com/LeagueAkari/LeagueAkari）**
  - 强项：基于 **LCU WebSocket**（而非轮询）的 gameflow/champ-select 驱动；成熟的 auto-select 英雄优先级、ban/pick 流水线；国服适配；ongoing-game 对局内选手数据查询。
  - 本项目可借鉴点：
    - champ-select/auto-gameflow 的**健壮性与断连恢复**（本项目走 0.5s HTTP 轮询，可作为参考增强，但**不建议直接替换**——架构不同：Akari 有自己的 RPC/WebSocket 层）。
    - 英雄优先级/banpick 策略配置的组织方式。
  - 已有相同功能：auto queue/accept/ban/pick、safe stop——本项目都已自实现，**不是空白**。
- **Syronss/syronss-lol-auto-assistant（bot.py / champions.py / models.py / tests/test_bot.py）**
  - 强项：单文件级的 auto queue/accept/ban/pick 流程，`champions.py` 提供英雄数据。
  - 对照：本项目的 `app/lol/data/*_catalog`（英雄/技能/召唤师技能目录）已覆盖其 `champions.py` 角色；本项目的 controller 已覆盖其 `bot.py` 流程。
  - 可直接搬移的：**有限**——更适合作为“auto queue/accept 状态机”的对照参考，而非代码级移植。
- **结论**：本项目的自动化并非“从零缺失”，而是“已自实现但未真测 + 死抽象（features 层）+ 无优先级配置”。优先级是**清理死代码、补配置、做真测**，而不是立即移植外部实现。

---

## 25. 建议的下一阶段（PHASE A0 Cleanup，仅建议，不执行）

1. **先固化未提交工作**：由负责人决定把当前 20 文件 +709/−150 的工作区提交/分拆成 commit，避免清理时误伤。
2. **删死代码（低风险）**：`ptt.py`、旧 `auto:` 段、重复 import、根目录探针、一次性 tools/viz/测试产物。
3. **决断 features 层**：删除或改造 `automation/features/*`，统一到 service 直接驱动 controller（二选一，不维持双轨）。
4. **补缺口**：语音 `cooldown.*` 分支（S2）；为 ban/pick 填真实英雄优先级（S4）。
5. **对齐文档**：重写 README（模块状态、真实目录、已删 kill_aggregator）。
6. **恢复测试环境**：在项目解释器里安装/确认 pytest，跑全量，确认 player/automation/online/overlay 新测试真的在收集与通过。
7. **真测（REAL_TESTED）门槛**：在有运行客户端时，逐个验证 Auto Queue/Accept/Ban/Pick/Safe Stop 与 Overlay/UI，再把状态从 IMPLEMENTED 推进到 REAL_TESTED。
8. **不要**在本阶段动第 23 节列出的稳定主链路。

---

## 26. 修改记录

- **代码修改**: **0**（未改任何业务代码）。
- **新增文件**: `docs/A0_PROJECT_AUDIT.md`（本报告，唯一允许的产物）。
  - 过程中为获取测试真实状态，曾向 `tools/` 写入临时文件 `tools/_a0_pytest.txt`（重跑失败的输出），该文件未作为交付物、可随清理删除；除此之外未新增/修改/删除任何项目文件。
- **删除文件**: 0。
- **运行的只读命令**: `git status/branch/log/diff --stat`、目录列举、源码 Read、grep、`pytest` 调用（因当前解释器未装 pytest 未成功，未安装依赖）。


---

# PHASE A0.5 — Cleanup Execution（追加）

> 执行日期：本会话。分支 `a0-cleanup`。
> 原则：不重构、不改业务逻辑；死代码优先归档而非永久删除。

## Git
- before HEAD: `fded3a6`（master）
- 保护性冻结提交: `41f291b chore: freeze pre-cleanup workspace`（在 `a0-cleanup` 分支上，完整保存清理前工作区）
- cleanup 提交: `db5937e chore(a0-cleanup): remove dead automation features layer, archive ptt, drop Faster-Whisper, prune stale config/docs`
- 工作区：cleanup 后干净。注：`git add -A` 将此前未跟踪的运行时产物（`data/tts_test/*.mp3`、一份 pdf）一并纳入提交，**未删除任何源码**，仅为归档噪音，可后续单独清理。

## 逐项执行

### CLEAN-01 移除 automation/features/* 死抽象
- 证据：全仓 grep，`.tick()` 在 app/ 内零调用；生产仅 `service.py` import+实例化；tests 仅 `test_lol_automation.py:509` 调用 `feature_status()`；无动态注册。
- 处理：
  - `app/lol/automation/features/` 整目录 `git mv` → `archive/automation_features/`（8 文件 + `__init__.py`，保留内容）。
  - `service.py` 移除 features import、`self._features` 字典、`_sync_feature_switches()` 及其全部调用。
  - `feature_status()` 改为直接读 `self._config`（单一事实源），输出 `{"auto_queue": {"enabled": bool}, ...}`，保持测试与 UI 契约不变。
  - 真实执行路径（`self._queue/_ready_check/_champ_select` 直接被 `_tick()` 调用）未改动。
- 结果：features 生产引用 = 0；真实 automation 执行链未变。

### CLEAN-02 归档 ptt.py
- 证据：生产 app/ 内无任何 import；仅 `tools/run_voice_music_test.py`、`tools/test_e2e_voice_music.py` 两个一次性探针脚本引用。
- 处理：`app/voice/ptt.py` → `git mv` → `archive/voice/ptt.py`。
- 影响：上述两个 tools/ 探针若运行会 import 失败（已知，非生产路径）；PTT 作为未来需求保留在 archive。

### CLEAN-03 移除 Faster-Whisper（负责人裁决）
- 生产引用 = 0（grep 确认 app/ 内无残留）：
  - `app/voice/asr.py`：删除 `FasterWhisperASR` 类、`initialize()` 中的实例化块；更新模块文档字符串。主链 Mic→RMS→Qwen3ServerASR→IntentRouter 未动。
  - `app/voice/__init__.py`：导出由 `FasterWhisperASR` 改为 `Qwen3ServerASR`。
  - `config/settings.yaml`：删除 `asr.faster_whisper` 块与 `engine` 注释中的 `| faster_whisper`。
- 配置引用 = 0；依赖引用 = 0（requirements.txt 本就未列 faster-whisper）。
- README：ASR 行改为仅 Qwen3-ASR（llama-server 主 / llama.cpp 备）。
- 测试：`tests/manual_test_asr.py`（import faster_whisper 的手动脚本）→ `git mv` → `archive/tests/manual_test_asr.py`。
- 残留（非生产，未动）：`项目工作报告.md` 为历史记录，保留原文。

### CLEAN-04 删除旧 auto: 配置段
- 证据：无任何代码读取旧 `auto:` 段；main.py 实际读 `automation:` 段。
- 处理：从 `config/settings.yaml` 删除整个 `auto:` 段（含其注释头）；新 `automation:` 段保留。

### CLEAN-05 删除 main.py 重复 import
- 证据：`from app.lol.cooldown import CooldownManager` 未使用，实际用 `as _CM`。
- 处理：删除重复行。

### CLEAN-06 README 更新
- 仅按真实代码重写：feature 表改用状态词（IMPLEMENTED/INTEGRATED/SKELETON，未把未真测标成 DONE）；移除已删的 `kill_aggregator.py`；结构补 automation/cooldown/ongoing/online/ui/player。

## 明确延期（REFACTOR LATER / FIX IN A2，未改动）
- GameAlarmEngine vs CooldownManager 两套计时：保留，未来统一 Timer Service。
- runtime 与 ongoing 对 ChampSelect 的双轮询：保留，未来统一抓取。
- Voice→Cooldown 桥接缺口：保留（规则产出 `cooldown.*` 但 `_execute_intent` 无分支），列入 A2 单独开发。
- Player 层多代数据访问、online music 源完善、UI/Overlay 真实对局目视验证。

## 验证
- `py_compile`：app/ + tests/ 共 153 文件全部通过。
- 全仓 grep：features / ptt / FasterWhisperASR / faster_whisper 在生产 app/ 内引用 = 0。
- 测试环境：当前解释器未安装 pytest/requests/pyyaml（按要求未擅自安装依赖），故未重跑 pytest；以最近一次归档结果（314 passed/7 skipped）为基线，本阶段未改动被测业务逻辑。

# Starlight 高级审查交接报告（Expert V2）

> 面向架构深水区，跳过表层规范，直接进入下一步重构的决策点。
> 若需第一份交接文档（项目全景、重构历史、基础求教区），见 `docs/REVIEW_REPORT_FOR_EXPERT.md`。

---

## 1. 项目最新状态与核心架构演进

| 项 | 值 |
| --- | --- |
| 仓库 | https://github.com/765756876fds/Starlight.git |
| 审查分支 | `a0-cleanup` |
| 当前 HEAD | `d283cda`（refactor(music): 抽离 MpvAdapter 协议，解耦 controller 对真实播放器的强依赖） |
| 技术栈 | Python 3.11 / PySide6 / mpv（独立进程 + IPC 命名管道）/ Node.js（音源桥接）/ SQLite |

### 近期三大重构（按时间序）

**① `887925e` — EventBus 并发契约收紧**

- 关键事件池改**单 worker**：严格 FIFO 保序，禁止在关键池投放长耗时任务（执行耗时 >1s 打告警日志）。
- 状态池实现**按 key 收敛合并**：同 key 事件覆盖旧值不追加；执行中同 key 新状态缓存为"最新待处理"，当前 handler 结束后续消费——最终收敛到最新状态，不丢更新。
- 关闭流程**有界等待**（默认 3s）：先 `shutdown(wait=False, cancel_futures=True)`，再轮询活跃线程，超时放弃并记录日志；**不再回退同步执行**，避免主线程被关闭期卡死。
- 修正了"优先级队列"的虚假注释：当前按到达顺序 FIFO 分发，未实现优先级。

**② `d75bfae` — 抽离 OnlineResolver（网络解析层）**

- 新模块 `app/music/online/resolver.py`：`OnlineResolver` 纯网络解析（多源搜索 → 候选排序 → 逐个源试 `get_play_url`），**不碰 mpv / 队列 / Qt 信号**；6 个选源纯函数（`_pick_candidates` 等）一并迁入，controller 顶部 re-export 保持兼容。
- Controller 引入全局自增 **`request_id`（代际号）**：本地 `play_song`、在线队列播放、`play_search_result` 三个入口统一递增；后台解析 worker 完成时校验代际号，过期即丢弃——旧网络解析永远不会覆盖用户最新的切歌/本地歌。

**③ `d283cda` — 抽离 MpvAdapter（播放器依赖接口化）**

- 新模块 `app/music/mpv_adapter.py`：`typing.Protocol` 定义 `MpvAdapter`，方法面 = controller 对 `self.mpv` 的**全部实际调用**（生命周期/播放控制/状态查询/事件订阅/`_properties` 属性缓存）。
- `MusicController.__init__(config, mpv_adapter=None)` 可选注入：注入时不再实例化真实 `MpvController`（保留默认逻辑）。
- 测试新增 `FakeMpvAdapter`：**零子进程、零 IPC**，播放路径毫秒级完成（`test_music.py::TestControllerMpvAdapterInjection` 已验证 `play_song` < 1s 且 `loadfile` 被调用）。

---

## 2. 当前深水区与求教方向（重点）

我们正准备啃最硬的骨头，以下三点是下一步重构的决策核心，希望专家指点方向。

### 2.1 Controller 状态机剥离：如何安全抽离 `PlaybackState`？

`MusicController` 仍有约 1700 行，核心播放状态机（Play/Pause/Stop、`_last_idle` 轮询基线、坏文件自动跳过、highlight 分段、防重入锁）全部还在类内。

求教点：

- **状态机与 MpvAdapter 的边界怎么划最清晰？** 目前 controller 直接读 `mpv.get_property("idle-active"/"playback-time"/"duration")` 来做"坏文件判定/自动切歌"。若抽 `PlaybackState`，这些**轮询采样逻辑**应该归状态机（定时拉取驱动）还是留在 controller（事件驱动回调）？
- **迁移的原子性**：状态机剥离涉及 `_last_idle` 上升沿语义（自动切歌唯一触发源）、`_missing_file_streak` 限次保护、`_confirm_playback` 异步确认线程。如何保证拆出去后行为完全不变（能否用 FakeMpvAdapter 做差分对拍：旧 controller vs 新状态机，逐场景输出对拍）？
- 状态机应该是**被动查询型**（controller 轮询时问它"现在该不该切歌"）还是**主动推进型**（自己持 timer 驱动）？与现有 0.8s 轮询线程的关系？

### 2.2 利用 FakeAdapter 做实况仿真测试

`FakeMpvAdapter` 已就位（记录调用、可注入返回值、可抛异常）。我们希望用它模拟极端场景，求教**测试方法论**：

- **IPC 延迟/超时仿真**：真实 mpv 卡顿时 `get_property` 会阻塞至 timeout。Fake 如何模拟"慢响应"（sleep 注入）以验证 `_try_restart_mpv` 的 fail-streak 计数与重启窗口？
- **mpv 进程崩溃仿真**：`is_running()` 突然 False / `get_property` 连续抛异常 → 验证自愈路径与降级运行不崩 UI。
- **网络未返回时的快速切歌**：`request_id` 已防旧解析覆盖，但 Fake 层如何注入"慢 resolve"，验证：A 歌解析中用户切 B 歌（本地）→ A 结果回来被丢弃；以及 B 也是在线歌 → 两个 worker 并发时的最终状态收敛。
- 这些仿真该放 unit 层（纯 Fake，秒级）还是 e2e 层（qtbot + Fake，验证 UI 状态变化）？

### 2.3 长生命周期（数小时运行）的资源清理与性能

桌面应用常驻数小时，求教长期运行的正确性：

- **EventBus 状态池收敛合并**：按 key 覆盖 + 最终收敛，长时间运行是否可能因"执行中同 key 缓存"或 `_pending_latest_state` 字典**只增不减**而内存膨胀？是否需要引入"同 key 缓存上限 + 陈旧丢弃"策略？
- **单 worker 保序 vs 长事务**：关键池单 worker 严格 FIFO，若某个 handler 意外长耗时（网络重试等），会**排队阻塞**后续关键事件（如播放状态切换）。如何在不破坏 FIFO 的前提下给关键 handler 加"执行时长护栏"？
- **轮询线程与 timer 的泄漏面**：`_poll_idle`（0.8s）、`_confirm_playback`（延迟线程）、`_schedule_skip_bad`（Timer）——测试隔离网已处理用例级清理，但**进程级长期运行**下，这些 daemon 线程是否存在累积/饿死风险？是否有比 `threading.Timer` 更可控的调度方式（如单一调度线程 + 事件队列）？

---

## 3. 本地测试与验证指令

- **启动**：`python main.py`（需自行配置 `config/settings.yaml`）。
- **纯逻辑层（核心门禁，极快）**：
  ```powershell
  $env:QT_QPA_PLATFORM='offscreen'; pytest tests/unit -m unit -q
  ```
  > 基线：约 1194 passed；3 个**已知历史遗留 failed**（`test_intent_music_clear_queue.py::test_stop_play_still_unknown`、`test_phase1a_room_history.py` 2 例），与本阶段重构无关，勿当回归。
- **Music 定向回归**：
  ```powershell
  pytest tests/unit tests/ui -k "music" -q
  ```
  当前：207 passed（含 FakeMpvAdapter 注入测试），仅 1 历史遗留 failed。
- **搬移/拆分层专项**：`tests/unit/test_online_pick.py`、`tests/ui/test_http_source.py`、`tests/ui/test_candidate_bypass.py`（52 passed）。

---

*报告生成时间：2026-10-22 · 由 Starlight 维护侧生成，供外部专家 Code Review 使用。*

# 全项目功能体检报告 —— Core 地基

> 只读调查（2026-10-12），不改代码。覆盖 app/core/ 全部 8 文件（event_bus 217 / command_bus 183 / state_store 126 / scheduler 205 / event_log 186 / event_replay 130 / logger 106 / log_bus 15）+ 全项目接线统计（python 正则扫描 43 个 py 文件，排除 _trash/）。

---

## ① 现有功能

| 组件 | 文件 | 职责 | 生产接线 |
|---|---|---|---|
| EventBus | event_bus.py | 模块间通信（事实广播） | ✅ 核心（main 15 处订阅 + objective_timer 6 + game_alarm 3 + tts/speech_judge/axiom_arc 各 1） |
| CommandBus | command_bus.py | 意图→命令→执行器 | ✅ 核心（26 个命令注册：music/controller 16 + main 10；voice/runtime 与 UI 执行） |
| StateStore | state_store.py | 全局共享状态（命名空间隔离） | ✅ 在用（30+ 处 set；query_handler 读 lcu.current_summoner） |
| Scheduler | scheduler.py | 定时任务 + 一次性计时 | ✅ 在用（runtime 2 个轮询任务 + cooldown/manager 一次性 CD 计时） |
| EventLog | event_log.py | 事件持久化 JSONL（按日分文件） | ✅ main.py:177 启动，subscribe_all 全量订阅 |
| EventReplay | event_replay.py | 事件回放（离线） | ⚠️ 生产零接线（仅 tools/ + 测试用） |
| Logger | logger.py | JSONL 日志（文件+控制台） | ⚠️ 半接线（main 创建，业务模块 0 写入） |
| LogBus | log_bus.py | Qt 信号日志（UI） | ✅ main.py:49 → LoadingPanel 日志框 |

---

## ② 实现方式

### EventBus（重点展开：订阅/发布设计）

```
subscribe(event_type, handler)      # 加锁 + 去重（handler not in list）
subscribe_all(handler)              # 通配符订阅（EventLog 唯一生产使用者）
unsubscribe(event_type, handler)    # 加锁移除
publish(event)                      # 同步分发：收集 handlers → 逐个 try/except
publish_async(event)                # 每事件开一个 daemon 线程
get_history() / clear_history()     # 内存 1000 条环形
```

**设计特点：**
- 锁保护订阅表与历史；发布时**锁外**执行 handler（不持锁分发，防死锁）✅
- 单 handler 异常被捕获，**不影响其他订阅者** ✅
- 事件优先级字段（HIGH/NORMAL/LOW）**定义了但 publish 从不排序**——publish 直接按收集顺序执行，priority 是死字段
- publish_async 无线程池、无上限——高频异步事件会线程爆炸（生产很少用，低危）

**⚠️ 订阅泄漏 / 重复订阅风险：**
| 项 | 现状 | 风险 |
|---|---|---|
| unsubscribe 调用 | **全项目仅 2 处**（生产 0 处，只有测试） | 🔴 生产订阅者**从不退订** |
| 防重 | subscribe 按 handler 身份去重 | 🟡 同名函数去重有效，但 **lambda/闭包每次 new** 的订阅（如循环里 subscribe）防不住 |
| 生命周期 | 仅 shutdown() 时全清；各模块**无析构退订** | 🟡 若模块热重载/重建（现无此场景），会累积 |
| 现状影响 | 单例进程常驻，订阅一次跑到底 | 🟢 当前**无实际泄漏**（进程不重建模块）；风险是"未来加动态模块/热重载时踩坑" |

结论：**当前不泄漏（单例常驻 + 无重建），但有结构隐患**——2 处 unsubscribe 全是测试，生产没有任何退订路径。

### CommandBus

```
register(command_type, handler)     # 一对一（同类型后注册覆盖先注册）
add_middleware(mw)                  # 中间件，返回 False 中止
execute(command)                    # 中间件 → handler；异常 re-raise
execute_async(command)              # daemon 线程执行
```

- 26 个命令已注册（音乐 16 + 自动化/cooldown/player 10）；命令来源：voice/runtime（语音意图）、UI
- **半成品：`add_middleware` 生产零使用**（日志/权限/确认中间件全部没接）
- **半成品：`Command.require_confirmation` 字段定义但零读取**——"需确认命令"机制从未实现
- execute 异常 **re-raise**（与 EventBus 的吞异常相反）——调用方必须自己 try（main.py:528 handler 等已包）

### StateStore（重点展开：共享状态 + 线程安全）

**共享状态清单（生产实际写入）：**
| 命名空间 | 键 | 写者 | 读者 |
|---|---|---|---|
| SYSTEM | running | main | — |
| GAME | is_active / game_time / game_mode / map_name / current_player / players | runtime | — |
| LCU | connected / gameflow_phase / raw_phase / current_summoner / champ_select | runtime | query_handler（唯一 get 消费） |
| MUSIC | volume / playing / current_song / highlight_mode / play_mode / song_count | controller | — |
| ALARM | active_count | game_alarm | — |
| VOICE | enabled / asr_enabled | voice/runtime | — |

**线程安全分析：**
- set/get 锁内读写 ✅
- ⚠️ **set 的通知在锁外**：`set()` 锁内比较 old_value，锁外 `_notify()`——两个线程并发 set 同一键时，通知可能乱序/重复/丢失。**当前无 on_change 生产用户，未爆炸；一旦启用即踩**
- ⚠️ **get_namespace() 不过滤 `{key}__updated_at`**：返回 dict 含内部时间戳键（snapshot() 过滤了，get_namespace 漏了）——不一致小 bug
- **半成品：`on_change` 监听器生产零使用**（写者 7 处、监听 0）——"状态变更通知"机制设计过但没接

### Scheduler（重点展开）

```
单线程 _loop（0.1s tick）→ 收集 should_run 任务 → 逐个 task.run()
add_task(interval 轮询) / schedule_once(一次性，cooldown CD 计时用) / cancel_once
run_once(手动立即执行) / enable/disable_task / list_tasks
```

- **异常处理 ✅**：task.run() 内 try/except，单任务异常不影响循环，错误计数 + last_error 可查
- **⚠️ 任务堆积风险**：单线程串行执行，**无超时保护**——某个任务阻塞（如网络请求挂起）→ 后续所有任务全部推迟（0.1s tick 变"卡死"）。ScheduledTask.run 的锁只护状态，护不住执行时长
- **⚠️ 双跑竞态**：should_run 锁外读 + run 锁内写，`run_once` 与 `_loop` 并发调同一任务可能双跑（生产单线程为主，低危）
- 生产任务：runtime 2999 轮询 + LCU 轮询（add_task ×2）；cooldown manager 每个 CD reminder 一个 schedule_once（清理干净，cancel_once 覆盖）

### EventLog（重点展开）

```
main 启动 → subscribe_all(_on_event) → 每事件：脱敏 → JSONL 追加 data/events/YYYY-MM-DD.jsonl
```

- **脱敏 ✅**：SENSITIVE_KEYS（token/password/secret/api_key/authorization/cookie/session）递归 5 层深度，命中替换 ***REDACTED***
- **防循环 ✅**：带 `_replay` 标记的事件跳过写入（Replay→EventBus→EventLog 不循环）
- **异常隔离 ✅**：写入失败只打 print，不炸 EventBus；JSON 序列化失败降级写简化版
- **存哪**：data/events/ 按日分文件；`count_events()` 只数行数，**无查询/回放读接口**（回放能力在 EventReplay，需手动拼路径）
- **⚠️ 无文件大小上限**：`/lol-gameflow` 等高频事件一局可能几 MB，跨天文件只追加不轮转

### EventReplay
- 离线工具：读 JSONL → 恢复 Event → 按原序 publish（带 _replay 标记防写回）
- 生产零接线（tools/test_replay_verify.py 手动用）；**无时间间隔模拟**（注释明说"尽可能快"）——回放调试可用，重现场景不真实

### Logger（logger.py）
- main.py:64 `get_logger()` 创建，**业务模块 0 写入**（全项目 print 是事实日志通道）
- 半接线：全局 Logger 空转，文件日志基本是空的；EventLog 承担了真正的持久化

---

## ③ 对标（成熟桌面应用的事件/状态管理）

| 能力 | 对标（Qt / Redux / ROS / APScheduler） | 本项目 | 差距 |
|---|---|---|---|
| 订阅生命周期 | Qt 自动断连（QObject 析构）、ROS 弱引用 | 生产**零退订**（2 处 unsubscribe 全在测试） | 🟡 常驻单例下不炸，动态化会炸 |
| 事件优先级 | 严格排序执行 | 字段有、**排序无**（死字段） | 🟢 小事 |
| 状态订阅 | Redux selector / Vue watch（精确订阅+去重） | on_change 存在但**零用户**；get_namespace 漏过滤 __updated_at | 🟡 半成品 |
| 任务调度 | APScheduler 超时/并发控制 | 单线程无超时，一个阻塞全卡 | 🟡 网络任务挂起=全调度冻结 |
| 日志 | logging 分级/轮转/结构化 | Logger 空转，print 为主；EventLog 有脱敏 ✅ 无轮转 | 🟡 |
| 可观测 | 事件浏览器/回放 UI | JSONL 全量 ✅ + 手工回放工具 | 🟢 缺 UI |

---

## ④ 增补候选

| 候选 | 标注 | 说明 |
|---|---|---|
| **StateStore.get_namespace 过滤 __updated_at**（1 行，与 snapshot() 对齐） | 🟢 好用且能做 | 明确小 bug，零风险 |
| **EventLog 文件轮转/大小上限**（超 N MB 切文件或清旧） | 🟢 好用且能做 | 防高频事件撑爆 data/events/ |
| **Scheduler 任务超时保护**（task.run 包 timeout watchdog，超时打日志不阻塞后续） | 🟡 能做但工作量大 | 网络任务（SGP/GSM/在线音源）挂起时防全调度冻结；改动集中在 scheduler.py |
| **CommandBus 中间件真正接入**（现有 add_middleware 零使用——接一个"日志中间件"） | 🟢 好用且能做 | ~10 行，顺便让 command 也有留痕 |
| **EventBus 订阅可观测**（subscribe 计数/来源打日志；shutdown 统一退订） | 🟢 好用且能做 | 防未来泄漏；~15 行 |
| **业务模块接入 Logger**（把关键 print 换 logger.warning/error） | 🟡 能做但工作量大 | 全项目 print 数百处，收益中（现状 EventLog 已兜底） |
| **EventReplay 时间间隔模拟**（按原始 timestamp 间隔回放） | 🟡 能做但工作量大 | 重现场景更真实；现"极速回放"调试可用 |
| **StateStore.on_change 接入**（比如 UI 订阅 GAME.players 变化自动刷新） | ⚪ 花架子 | 现有 UI 靠 QTimer 轮询，改事件驱动收益低风险高 |
| **EventBus 优先级排序落地** | ⚪ 花架子 | 现无需求（订阅者数量少，顺序无实际影响） |

---

## 附：半成品 / 死代码清单（本层）

| 项 | 状态 | 证据 |
|---|---|---|
| **LoadingPanel 9 模块状态灯** | 🔴 死 UI（已确认） | `set_module_status` 全项目 **仅定义、零调用**（grep 唯一命中 loading_panel.py:52）——启动页 9 个状态灯永远"○ 等待..."，只有日志框真实工作 |
| CommandBus.add_middleware | 半成品 | 定义 + 文档齐全，生产 0 调用 |
| Command.require_confirmation | 半成品 | 字段定义，0 读取 |
| StateStore.on_change | 半成品 | 定义 + _notify 完整，生产 0 注册 |
| Event.priority 排序 | 死字段 | HIGH/NORMAL/LOW 定义，publish 不排序 |
| Logger 全局日志 | 半接线 | main 创建，业务 0 写入 |
| EventReplay | 工具（正常） | 离线调试用途，非缺陷 |
| CooldownPanel（82 行） | 死代码（UI 层） | 计时器 Tab 被注释隐藏，无引用 |

## 建议优先做（若采纳）
1. **StateStore.get_namespace 过滤**（🟢 1 行 bug）
2. **EventLog 轮转**（🟢 防数据膨胀）
3. **CommandBus 日志中间件**（🟢 让命令链路可观测）
4. **Scheduler 超时保护**（🟡 网络任务卡死时防全局冻结——与 Ongoing 网络层问题相关，优先级上调）

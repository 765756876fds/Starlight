# Starlight — 外部专家审查交接报告

> 本文档面向受邀参与 Code Review 的外部技术专家，提供项目现状、近期重构说明与已知深水区清单，帮助快速上手。所有 commit、行数与测试数据均为实测核对值。

---

## 1. 项目基础信息与新地址

| 项目 | 信息 |
| --- | --- |
| 仓库地址 | `https://github.com/765756876fds/Starlight.git`（原 `lol-music-agent`，已更名，旧链接失效） |
| 审查分支 | `a0-cleanup`（最新 Commit：`215aef8`） |
| 技术栈 | Python 3.11 / PySide6 / mpv（独立进程，命名管道 IPC）/ Node.js（在线音源 JS 桥接）/ SQLite |
| 项目性质 | 桌面端聚合应用：LoL 游戏辅助 + 音乐/影视聚合播放 |

核心进程模型：UI 主线程（PySide6）与 mpv 播放器为**两个独立进程**，通过命名管道同步 IPC 通信；在线音源经 Node.js 桥接层解析 URL 后交给 mpv 加载。

---

## 2. 近期重大重构与修复方向（今日提交链）

项目近期完成了一轮底层稳定性治理与测试体系重塑，以下 Commit 建议优先审阅：

### 2.1 UI 线程防阻塞（窗口拖拽假死根治）

| Commit | 内容 |
| --- | --- |
| `ad1ef72` | 挂载 PySide6 审计技能，将 `music_panel.py` 中榜单/歌单/历史等 **5 个双击播放槽全部异步化**——UI 只发信号，网络解析在后台线程完成，消除点击在线歌曲时的主线程卡顿。 |
| `058956e` | 进一步**隔离 mpv 同步 IPC**：`mpv.loadfile()` 移入后台 worker 执行，播放成功后的列表刷新用 `QTimer.singleShot` 延后，`_refresh` 轮询改为短超时读——根治拖拽窗口"半秒卡住后恢复"的隐性阻塞。 |

设计要点：所有网络请求、mpv 写指令均不在主线程执行；跨线程结果一律通过 Qt Signal 投递回主线程，遵守"Signal 必须在 QObject 归属线程 emit"的规则。

### 2.2 生命周期与死锁治理

| Commit | 内容 |
| --- | --- |
| `0b8d943` | 全局治理收尾：修复 `waveform_slider.py` / `main.py` **QTimer 未 stop 与空转**；清理可变默认参数、字典重复键；为静默 `except` 补 warning 日志。 |
| `aab691f` | 修复**音乐暂停态死锁**：暂停后双击切歌失效——切歌入口强制重置 `_paused=False` 并解除 mpv pause 属性，确保 loadfile 总能覆盖旧状态开始播放。 |
| `ccd84b3` | 修复**电影同类死锁**：暂停状态下切集/切电影不自动播放——`loadfile` 后强制 `set_property("pause", False)`。 |

整体原则：**"切歌/切集 = 新播放请求"，任何播放入口不继承旧的暂停状态**。

### 2.3 工程化与日志规范化

| Commit | 内容 |
| --- | --- |
| `0b8d943` | 全仓接入标准 `logging`，**替换约 600 处 `print`**，生产环境日志统一落盘（app.log）；同时执行 `ruff --fix` 统一 typing 现代化与 import 排序。 |

### 2.4 测试基建重塑（今日最新）

| Commit | 内容 |
| --- | --- |
| `215aef8` | 测试体系物理分层：`tests/unit/`（110 个纯逻辑测试文件）+ `tests/ui/`（39 个 UI 测试文件）+ `tests/e2e/`（pytest-qt 交互自动化）；引入 pytest-xdist 多进程并行、pytest-cov 覆盖率门禁、autouse 级防污染隔离网（每用例后 `QApplication.processEvents()` + 残留 QWidget 清理）。同步修复移动引发的 26 处 `__file__` 层级偏移与 1 处跨文件 import。 |

---

## 3. 已知深水区与求教方向（重点审阅区）

以下三处是当前项目最希望外部专家给出专业意见的地方：

### 3.1 上帝对象问题：`app/music/controller.py`

- 实测 **1703 行**，单文件耦合了：播放状态机（`_paused`/`_is_playing`/`_loading` 等状态位）、在线音乐网络解析（榜单/歌单/历史）、mpv IPC 调度、队列管理。
- 历史峰值阶段约 2000 行，经多轮治理降到当前规模，但**拆分尚未动手**。
- 求教：按什么边界拆分最合理（状态机 / 网络解析 / IPC 适配 / 队列四层？），以及拆分过程中的状态一致性迁移策略（避免拆分引入新的状态死锁）。

### 3.2 EventBus 并发模型

- 现状（`app/core/event_bus.py` 已核实）：`publish(event)` 是**同步分发**——主线程逐个调用 handler，任一长 handler 会阻塞发布者；`publish_async(event)` 是异步——按事件类型路由到**隔离双池**（状态池：拥堵时丢弃新事件，高频状态合并语义；关键池：保序投递）。
- 求教：如何**安全地全量异步化** `publish`，避免长 handler 阻塞发布者？同步/异步混用的边界怎么定（哪些事件必须同步、哪些必须异步）？双池模型的容量与背压参数如何调优？

### 3.3 测试体系的可持续性

- 历史遗留：Qt 全局状态污染（定时器未停、mpv 实例未销毁）导致极少数 UI 用例在**并行运行时仍偶发死锁**——已通过物理分层 + `--dist loadfile` 隔离到可接受范围，但未根治。
- 求教：桌面端（真实 Qt 事件循环）做 UI 自动化测试的更优雅方案？如何让 UI 测试具备确定性与可并行性（如逐用例独立 QApplication、事件循环超时护栏、MPV mock 策略）？

---

## 4. 如何在本地运行与测试

### 启动

```powershell
python main.py
```

首次运行需自行创建并配置 `config/settings.yaml`（可参考 `config/settings.example.yaml` 模板；真实文件含用户 API Key，不入库不入包）。

### 测试（Python 3.11，`C:\Program Files\Python311\python.exe`）

```powershell
# 纯逻辑单测（unit 层）—— 实测 1192 passed / 3 failed / 7 skipped，约 1 分钟
$env:QT_QPA_PLATFORM='offscreen'
python -m pytest tests/unit -m unit -q

# UI 交互测试（ui 层，需 offscreen 平台）
python -m pytest tests/ui -m ui -q
```

已知说明：

- **3 个 unit 失败为历史遗留断言过时**（与业务演进不同步，非本次重构引入），清单：`test_intent_music_clear_queue.py::test_stop_play_still_unknown`、`test_phase1a_room_history.py::test_loader_uses_account_service`、`test_phase1a_room_history.py::test_failure_isolation_one_player`。
- **ui 层存在已知收尾卡点**：多文件并行跑到约 93% 时偶发停滞（历史 Qt 状态污染遗留，已隔离在 ui 层，不影响 unit 层），遇到时按需杀掉残留 python 进程即可。
- 并行配置在 `pytest.ini`（`-n auto --dist loadfile`）：同一文件内用例串行，不同文件间并行，避免共享单例冲突。

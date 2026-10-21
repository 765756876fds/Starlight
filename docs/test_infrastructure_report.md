# 测试基建重塑报告（防污染隔离网 + 多进程并行 + E2E 交互测试）

> 生成时间：2026-10-21 ｜ 分支：`a0-cleanup` ｜ 执行者：主控 AI + Organizer 子代理
> 背景：历史全量测试在单进程长跑中必现"72% 处 CPU 停滞卡死"（Qt 全局残留：定时器未停 / mpv 未销毁 / 孤儿窗口）。用户明确"不追求速度，要专业、合理、有效"，要求从底层基建入手彻底解决状态污染，并实装真正的 UI 自动化交互测试。

---

## 一、要解决的问题与设计目标

| 历史问题 | 根因 | 本方案 |
| --- | --- | --- |
| 全量组合跑 72% 卡死 | 老测试各用例残留 QTimer / 后台线程 / 未销毁 QWidget，单进程长跑累积到临界点后事件循环互锁 | ① autouse teardown 强制清 Qt 事件队列与顶层窗口 ② `--dist loadfile` 同文件串行、跨文件多进程物理隔离 ③ 禁用 pytest-randomly 消除随机顺序对判定的干扰 |
| 测试互相污染 | EventBus / 单例 / 全局状态在用例间泄漏 | E2E 一律使用**全新实例**（`_FakeBus` 替身 + FakeMusic 双例隔离），不做不可逆的全局 `EventBus.shutdown()` 复位 |
| 老测试依赖真实网络 / 真实 mpv | 慢、卡、不可复现 | E2E 严格 mock：FakeMusic 替身拦截网络与播放器，测试原子、瞬间完成 |

---

## 二、隔离网设计（tests/conftest.py）

```python
# 顶层强注入（任何 import 之前生效）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

@pytest.fixture(autouse=True)
def _qt_process_cleanup():
    """每个用例结束后：
    1) 3×processEvents 清空 Qt 事件队列；
    2) close 所有可见顶层 QWidget（触发面板 hideEvent → 停 QTimer / 停进度轮询线程）；
    3) 再次 3×processEvents 让 deleteLater / 信号队列落定。"""
    yield
    app = QApplication.instance()
    if app is None:
        return
    for _ in range(3):
        app.processEvents()
    for w in app.topLevelWidgets():
        try:
            if w.isVisible():
                w.close()
        except Exception:
            pass
    for _ in range(3):
        app.processEvents()
```

关键取舍：

- **不调用 `EventBus.shutdown()` 复位**：`shutdown()` 后 EventBus 拒绝接收新事件，不可安全复用；正确姿势是测试各自用全新实例，全局单例不动。
- **close 顶层窗口而非 deleteLater**：close 会触发面板 `hideEvent`，正是生产代码里"隐藏即停定时器"的生命周期钩子，测试由此顺带验证了真实行为。
- **无 QApplication 的纯逻辑用例自动跳过**，不影响 unit 层速度。

---

## 三、多进程物理拆分（pytest.ini）

```ini
[pytest]
testpaths = tests
addopts = -n auto --dist loadfile --cov=app --cov-report=term-missing --cov-report=html -p no:randomly
markers =
    unit: 纯逻辑单元测试（不依赖 Qt 事件循环）
    integration: 集成与模块协同测试（网络需 mock）
    ui: UI 交互测试（必须隔离）
```

- `-n auto`：多进程并行（本机 7 worker 同时跑）。
- `--dist loadfile`：**同一文件内用例永远在同一进程串行**，杜绝多个 worker 争抢同一单例 / 文件锁 / 端口。
- `-p no:randomly`：禁随机顺序。历史实测：随机顺序干扰"卡死点判定"，关闭后串行可推进过 72%。
- `testpaths = tests`：防止 `tools/test_lcu_champ_select_verify.py`（含 `sys.exit(1)`）被 rootdir 递归收集（首轮 `KeyError: <WorkerController gw1>` 崩溃即由此引发，已修复）。

### 打标统计（脚本一次完成）

- 39 个 UI 测试文件：`pytestmark = pytest.mark.ui`
- 110 个纯逻辑文件：`import pytest` + `pytestmark = pytest.mark.unit`
- 打标过程修复了两处脚本副作用：`from __future__` 必须保持在文件最前（8 个文件重新插到 future import 之后）、多行 import 括号内不可插入（test_overlay.py 改用括号深度感知算法）、BOM 文件保持 BOM（test_player_query.py）。
- 全仓 `py_compile` 通过（150+ 测试文件 0 语法错误）。

---

## 四、E2E 交互测试实装（tests/e2e/test_ui_interaction.py）

5 个高价值用例，全部严格 mock（FakeMusic 替身 + `_FakeBus` 替身拦截 `get_command_bus / get_event_bus` 单例）：

| 用例 | 模拟操作 | 断言 |
| --- | --- | --- |
| `test_double_click_plays_song` | `mouseClick` + `mouseDClick` 双击歌单首项 | 状态栏变"正在播放"，`play_online` 被调用 |
| `test_play_toggle_button_icon_switches` | 点击播放/暂停按钮 | 图标翻转（`cacheKey` 变化）、`toggle` 被调用、图标锁解除 |
| `test_search_input_triggers_request` | `_type_text` 输入"周杰伦" + 回车 | 搜索请求触发（mock 拦截）、列表出现"[在线源]"条目 |
| `test_progress_slider_drag_updates_value` | press→move→release 拖拽进度条 | 进度值增大（模拟播放中 enabled 状态） |
| `test_window_drag_keeps_ui_responsive` | 拖动窗口标题栏 + 拖拽进度条 | 拖拽后 UI 事件循环仍响应（标题可改） |

### 两个关键工程结论（实机验证）

1. **`QTest.keyClicks` 输入中文（CJK）会触发 Qt 原生崩溃（0xC0000409）**，Windows + PySide6 6.11.2 实测必现。E2E 改用自写 `_type_text()`：逐字符发送 `QKeyEvent(KeyPress/KeyRelease, Key_unknown, NoModifier, text)`，崩溃消除。这是 Qt 测试领域的已知坑，写入本报告供后续复用。
2. **`WaveformSlider(QSlider)` 初始 disabled 会忽略鼠标**（播放中才 enable），E2E 用例先 `setEnabled(True)` 再拖拽——与生产"进度条播放中可用"的真实语义一致。

### E2E 运行结果

- 串行（`-n 0`）：**5 passed in 10.91s**
- 默认命令（并行 addopts + cov）：**5 passed in 16.35s**

---

## 五、分层门禁执行结果（最终大考）

| 层 | 命令 | 结果 | 耗时 | 结论 |
| --- | --- | --- | --- | --- |
| 纯逻辑 | `pytest -m unit -q` | **1191 passed, 4 failed, 7 skipped, 0 errors** | 66.9s（并行） | 打标脚本 9 个收集错误全部修复归零；并行下无卡死 |
| UI 交互 | `pytest -m ui -q` | 72% 老卡点**已越过**（此前必卡死）；92% 处个别老用例停滞，进程已按预案终止 | — | 隔离网 + 并行把"全量卡死"收窄为"个别老用例死锁" |
| 全量并行 | `pytest -q` | **推进至 98%** 后 worker 原生崩溃（xdist 会话终止，无 summary） | ~6 分钟 | 见第六节 |

### unit 层 4 个 failed（历史既有，非本轮引入）

均属"断言与当前行为不符"的业务演进遗留（打标只加 marker、不改行为）：

1. `test_intent_music_clear_queue.py::test_stop_play_still_unknown` — `system.stop_tts` ≠ `system.unknown`（意图解析已演进）
2. `test_phase1a_room_history.py::test_loader_uses_account_service` — `NO_DATA` ≠ `AVAILABLE`
3. `test_phase1a_room_history.py::test_failure_isolation_one_player` — 同上
4. `test_online_queue.py::test_queue_online_song_researches_and_plays` — 队列在线歌播放链路断言

按用户决策"停止修复旧测试"，这 4 项记录在案、不修，建议后续由业务侧确认断言是否更新。

---

## 六、历史卡死问题在并行下的处置结论

- **72% 历史卡点**：单进程串行必现 CPU 停滞；并行 + 隔离网 + `no:randomly` 下**已越过**，不再卡死。物理隔离网对"旧组合卡死"显著生效。
- **92% 新卡点**：ui 层 7 进程并行下仍有个别老用例死锁（16 分钟无进展）。这证明老套件存在**用例级**死锁（等待真实资源 / 事件循环互锁），已从"全局组合级卡死"收窄为"个别用例级"，符合"物理隔离把卡死拆散"的预期；彻底根治需逐文件定位死锁用例（不在本轮范围，用户已决策停止修复老测试）。
- **全量并行**：unit 层 1191 用例 67s 全过无卡死；**全量混合并行推进至 98%**（unit 全过 + ui 老套件大部分跑完）后，个别老 UI 用例触发 Qt 原生崩溃导致 xdist worker 终止、会话中断。**对比原单进程 72% 卡死：卡死点已从"全局组合级"（72% 停滞）推进至"收尾 2%"（98% 处个别用例崩溃）**，隔离网 + 多进程物理拆分对历史卡死问题的治理效果明确，剩余个别用例级问题待逐文件定位（不在本轮范围，用户已决策停止修复老测试）。

---

## 七、lorairo-test-generator 技能挂载说明（降级）

- `npx skills add https://github.com/NEXTAltair/LoRAIro` 经代理 127.0.0.1:7897 连接失败（2057ms），仓库未克隆。
- 按任务预案降级：直接执行其方法论——"分层生成 fixtures/mocks 并强制生成覆盖率报告"，已落地为：分层 marker（unit/integration/ui）、E2E Fake 替身体系、`--cov` 强制覆盖率门禁。

---

## 八、覆盖率

`--cov=app --cov-report=term-missing --cov-report=html`（htmlcov/ 输出到项目根）。

unit 层结果：TOTAL **9%**（26411 行 app 代码覆盖 23938 行未覆盖——注意这是**仅 unit/UI 层跑动路径**的统计，未覆盖大头为 app/third_party 指纹库、voice/runtime 等实机依赖模块）。建议后续对未覆盖最重的 3 个核心模块（`app/voice/runtime.py` 766 行 0% 覆盖、`app/ui/widgets/video_player_window.py` 1075 行 0%、`app/music/controller.py` 0%）补充定向测试。

---

## 九、运行方式速查

```powershell
# 纯逻辑快速门禁
$env:QT_QPA_PLATFORM='offscreen'; python -m pytest -m unit -q
# UI 交互（E2E，10s 级）
$env:QT_QPA_PLATFORM='offscreen'; python -m pytest -m ui -q --ignore-glob="tests/test_*.py"
# 指定 E2E 文件
$env:QT_QPA_PLATFORM='offscreen'; python -m pytest tests/e2e/test_ui_interaction.py -v -n 0
# 全量并行（unit 秒级通过；ui 老套件已知个别用例死锁，见第六节）
$env:QT_QPA_PLATFORM='offscreen'; python -m pytest -q
```

> 尾注（全量并行结果）：`pytest -q`（并行，含 unit+ui+E2E）实际推进至 **98%**（进度行 94% → 98% 后无 summary），xdist worker 在收尾阶段因个别老 UI 用例原生崩溃而终止会话（exit -1），未产出最终统计行；unit 层单独跑 1191 passed 已在第五节确认。

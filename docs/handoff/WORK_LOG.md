# Starlight 2.0 — 工作历史

## 2026-11-05 统一通知中心与灵动岛式语音反馈

**授权与保护**：用户完整授权本批相关源码、UI、测试，禁止提交、推送、打包和清理。开始状态 `a0-cleanup` / `e686786dd64c6fe5243c28377a0fbd39900ede47`，16 tracked 修改、36 未跟踪文件，暂存区空。先在仓库外保存状态、文件指纹和源文件备份，再建立 HEAD 加实际工作区修改的隔离测试副本。保留既有 LoL、TTS、识曲、游戏时间、脚本及全部产物。

**本轮源码清单与用途（18 份）**：

| 文件 | 用途 |
| --- | --- |
| app/core/notifications.py（新） | 有限内存模型、操作上下文、结果解释、合并和清空；观察失败不改变业务返回 |
| app/core/command_bus.py | 可选通知标识及现有异步命令关联；保留原执行与返回契约 |
| app/voice/runtime.py | 识别/执行/结果接入、真实播放器复查、空文本与设备状态、重复音频及过期结果保护 |
| app/speech/speech_queue.py | 独立播报结果关联，取消/过期/失败可见，保留原优先级与业务执行 |
| app/speech/tts.py | 线程捕获各自请求及代次，失败归原操作；保留原后端、重试及日志 |
| app/ui/panels/notification_panel.py（新） | 200 条列表、时间、状态、说明、完整详情、清空及主线程渲染 |
| app/ui/panels/console_panel.py | 兼容类名，产品入口改为通知中心，不再订阅原始日志 |
| app/ui/widgets/notification_stream.py（新） | Qt 排队回传、监听释放 |
| app/ui/widgets/voice_island.py（新） | 当前语音胶囊、实际识别文字、动画、收起、播报附注、点击通知入口 |
| app/ui/theme/notification_style.py（新） | 集中语义颜色、图标角色与未来浅色接口 |
| app/ui/widgets/icon_utils.py | 补齐成功、失败、提醒图标，避免回落为默认音乐图标 |
| app/ui/panels/status_bar.py | 胶囊与设备/客户端状态，失败归属补报，窄窗口与两行高度 |
| app/ui/widgets/main_tabs.py | 控制台重命名与已有通知页导航，主 TAB 仍为五个 |
| app/ui/main_window.py | 胶囊点击接到已有通知页 |
| app/ui/panels/music_panel.py | 搜索、队列操作完成通知，搜索异常日志 |
| app/ui/dialogs/identify_dialog.py | 一次识曲及后续搜索关联、成功/失败/取消通知、异常日志 |
| app/ui/widgets/video_player_window.py | 字幕真实完成回调通知，不改解析及播放接口 |
| app/ui/panels/settings_panel.py | 插件重载结果通知，不改变插件或网络配置 |

**测试文件（7 份）**：新增 `tests/unit/test_notifications.py`、`tests/ui/test_notification_center.py`、`tests/ui/test_notification_integrations.py`；更新现有 `test_console_panel.py`、`test_voice_feedback_view.py`、`test_tts_feedback.py` 和 `test_main_tabs.py`。旧“控制台必须显示日志/弹出语音反馈框”的预期与本次明确产品变更冲突，改为验证不投射技术日志、保留日志落盘、单记录合并、真实结果、历史、导航和 Qt 线程；没有跳过或弱化业务执行断言。

**实现与失败转通过证据**：

- 业务已暂停、TTS 合成/播放失败：记录仍为成功，只更新播报附注，handler 只执行一次。
- `notification-edge-before`：重复音频多一条处理中记录，以及播报提供者异常缺附注，2 failed；修复后相关回归通过。
- `observer-isolation-before`：通知观察故障改变已成功的业务返回，1 failed；加故障隔离后 149 项相关回归通过。
- `tts-owner-before`：两个独立线程中，旧合成错误晚到被归到新请求，1 failed；固定各自操作及代次后 TTS/通知回归通过，状态栏补报也使用原失败所属操作。
- 胶囊比较完整不可变记录，不仅比较时间戳，避免 Windows 粗时间粒度漏更新；主窗口真实 QStatusBar 布局验证了两行高度，修复前 38px，修复后 44px。
- 当前游戏顶部时间刷新原已实现，6 项测试通过，本轮未改 CurrentGamePanel。
- 开发过程首批有 6 项失败：一个新增测试误用 Intent 的 scope 构造参数、五个技术文案降级为空；均已修正。曾误写不存在的 test_command_bus.py 路径导致退出码 4、未执行测试；后续使用实际测试文件。所有过程日志保留，不计为最终通过。

**真实命令与证据**（临时目录不是产品运行目录）：

```powershell
$q = Join-Path $env:TEMP 'starlight-notifications-kfd4b5qn'
& .venv\Scripts\python.exe -B "$q\verify_final.py" acceptance-final
& .venv\Scripts\python.exe -B "$q\run_real_plugin_check.py" tests/unit/test_plugin_refresh_sync.py::test_detect_plugin_type_real_files
& .venv\Scripts\python.exe -B "$q\render_scales.py"
# 在 $q\mirror 中执行，缓存只写仓库外：
& 'C:\娱乐助手\Starlight\.venv\Scripts\python.exe' -X "pycache_prefix=$q\pycache" -m compileall -q app main.py tests
git diff --check
git diff --cached --check
```

`verify_final.py` 重放实际逐文件定向清单，完整展开命令、退出码和用时保存在 `commands.jsonl`；不是全量 CI。真实插件补测只对实际 `.js` 文件进行分类，不执行、下载或替换插件，不访问数据库。Qt 预览使用隔离示例数据和系统现有中文字体，不代表真实设备或业务服务验收。

**边界与收尾**：不在 GUI 解析技术日志或根据文本关键词判业务成功，不新增持久化通知数据库或语音任务队列；旧接口无确认时保留待确认。没有读取/写入 .coverage，没有提交、推送、打包、系统网络或真实游戏操作。实际麦克风、扬声器、网络服务与完整 Windows 缩放体验仍需用户验收。

**证据目录**：`%TEMP%\starlight-notifications-kfd4b5qn` 保存 before.json、original-paths.json、before-files、this-turn.diff、测试日志/XML、界面预览及最终指纹。新增文件在开发过程中也可能有中间备份；施工前是否存在以 before.json/original-paths.json 为准。以本轮增量 diff 区分已有改动，不把整个 HEAD diff 都算成本轮成果。

**最终真实结果**：`acceptance-final` 合并回归 942 passed、0 failed、1 skipped、0 warnings，pytest 65.93s，进程退出码 0；`acceptance-final.xml` 为实际最终源码对应结果，完整参数在 `commands.jsonl`。新增用例为通知模型 28、通知 UI 15、业务接入 17，共 60。原跳过项为隔离目录无真实插件文件；`run_real_plugin_check.py` 针对实际目录的原断言另行 1 passed（0.51s），不是插件联网验证。过程中的 941/937/936 等结果包含重叠，不能累加。

最终布局相关 47 项通过；Qt 组件 280/480/960/1440 宽度测试、360/960 实际预览及 1/1.25/1.5 离屏缩放通过。实际 QMainWindow 状态栏内胶囊高度为 44px，未被压缩。`compileall -q app main.py tests` 的缓存写仓库外，退出码 0；`git diff --check` 和 `git diff --cached --check` 通过，Git 另有部分文件 CRLF 转换提示，不是 pytest warning。

最终工作区：26 tracked 修改、44 未跟踪文件，暂存区空、HEAD 不变。本轮实际修改 18 份源码、7 份测试、6 份维护文档；原有未跟踪文件无缺失、未参与本轮文件无额外变化、测试副本源码及测试指纹与工作区一致。旧安装包、数据库和备份原样保留。至此停止施工，等待源码实机验收。

## 2026-10-30：GitHub 同步前整理（本次已获 commit/push 授权）

提交前核验：本地与真实 origin/a0-cleanup 均为 6d42ed40f06617562ff2fea8059c7ab4fa40bf75，ahead/behind=0/0。默认分支“么西么西”不改，不自动合并。Git原配置代理127.0.0.1:7897未运行，单次命令临时绕过后安全fetch成功；未关闭TLS或修改全局代理。

重写README为面向用户的音乐、电影、LoL、本地语音产品介绍，注明在线识曲发送音频、TTS联网/密钥、模型和运行时不随Git提供、实机边界及已知测试失败。精确选择58个正式文件，排除Lite辅助脚本、数据库、备份、安装器、实验和凭据维护脚本。新增源码、测试、图标及语音说明均纳入依赖闭包。完整暂存快照导出到独立临时目录，测试不使用原工作区的私人配置/数据库/日志。

本次实际回归：27个文件/节点，727 passed、2 failed、0 warnings，退出1（52.87秒）。除既有movie pauser测试外，test_humming.py::test_runtime_dispatches_identify 的 SimpleNamespace 未提供真实CommandBus已有的has_handler；真实CommandBus的隔离调用一次成功，真实面板注册和打开测试通过。未修改业务代码、测试断言或替身以掩盖失败。编译、暂存差异和凭据扫描另行核对。此条记录为提交前证据，不代表CI通过或发行验收通过；提交/推送结果以最终核对SHA为准。

## 2026-10-30：已批准的两项定点修复

仅修复完整 ASCII 单引号引用命令误执行和音乐倍速 IPC 失败虚报成功。引用保护在归一化前完成，含跨行；英文词中撇号保留。set_property 透传原始协议响应，音乐 set_speed 使用 True/False/None 三态，仅成功才更新状态，UI 用 object 信号保留未知值，原语音三态反馈直接复用。其他 MPV 调用方仍忽略返回值，调用与外层返回行为由回归验证，未改生命周期或电影流程。

施工前 32 个 tracked 修改、47 个未跟踪文件，暂存区空；修改前快照为临时目录 `starlight-two-defects-9y730w0o`。三个原始探针和同名项目回归均先 3 failed，修复后 3 passed。新增 50 项单元回归、5 项真实 Qt 信号/槽测试。最后定向 82 passed、0 warnings、退出码 0。扩大到 13 个文件时 529 passed、1 failed；失败为 test_music.py::TestP0ConcurrencyAndRobustness::test_apply_stream_unifies_headers_and_state 未注入 movie pauser，在施工前控制器快照的独立子进程中同样复现，未修改该测试或安全阻断逻辑。新增 UI 测试最初因 QSignalSpy.wait 持有 GIL 导致 5 failed，独立探针证实后改为同样 3 秒上限的 Qt 事件驱动等待，所有结果断言保留。跨行引用也先复现后修复。

修改范围：intent.py、music/mpv.py、mpv_adapter.py、controller.py、ui/panels/music_panel.py；test_music_speed.py 仅明确成功场景的真实 IPC 响应（保留原断言），新增 test_voice_speed_result_regressions.py 和 test_music_speed_result.py；四份 handoff 追加记录。编译与两项 diff 检查通过。未打包、提交、推送或清理；真实 MPV/麦克风/游戏未验收。下一步仅审查与实机验收，不自动开始新施工。

## 2026-10-30：语音对抗性回归与执行反馈收口

基于原有脏工作区增量修复，未改 HEAD。先复现询问/负数误建计时、参数猜值、文本折叠丢领域、音量步长丢失和反馈假成功等问题；增加未执行任务/待播反馈有效性检查、回调一次性消费及歌单局部契约修复。新增 40 个测试函数，参数化后 158 项（其中 65 项验证能力矩阵示例，另 1 项核对行数）。相关回归同一条命令 771 passed、4 warnings；最后修复全局暂停/继续反馈绕过有效性检查，受影响回归 356 passed；新增测试文件单独 158 passed。这些结果重叠不相加。compileall、未暂存/暂存 diff --check 通过。详见 `docs/语音对抗性回归验收.md` 和 `docs/语音能力矩阵.md`。未打包、暂存、提交或推送，未启动真实媒体、麦克风或游戏操作。

## 2026-10-30：语音语义整治

在 HEAD 6d42ed4 及既有工作区成果上增量施工，修改前备份到临时目录。修正小数归一化、否定/转述/多命令边界、明确音乐域、参数追问及校验、异步反馈、ASR 代次与音频防重、自动同意别名和交闪技能提取；复用现有 cooldown.list 业务接口。新增 61 项语义测试，同一条相关回归 595 passed、4 warnings。未打包、提交或推送；实机待验证。文件范围与剩余边界详见 `docs/语音语义层整治验收.md`。

## 2026-10-26：第四轮集中修复

**工作目标**：恢复 R1-R4 + P0-A/B/C 功能，统一 lol_data 路径，补 Qwen Key 迁移。

**修改文件**：18 个 modified + 2 个新增测试文件。

**行为变化**：
- 用户数据目录分离（Program Files → LOCALAPPDATA）
- ASR 双后端回退（vulkan → cpu）
- 模型种子迁移（Program Files → 用户目录）
- SQLite backup API 一致性迁移
- lol_data_dir() 统一路径入口
- Qwen Key 自动迁移
- SSL certifi 显式 context
- mpv 路径失效自动回退
- current_game_panel c 变量初始化

**测试结果**：24 个 unittest 通过（mock）。

**未执行**：真机 ASR、真机 SSL、真机 SQLite 迁移、全量 pytest。

**未提交**：工作区保留修改，未 commit。

## 历史记录（待核实）

- 项目原名称 → Starlight 重命名：历史记录，待核实
- EventBus 并发改造：历史记录，待核实
- OnlineResolver / MpvAdapter 可测试性改造：历史记录，待核实
- LoL 英雄填充 / 绝活缓存刷新：历史记录，待核实
- 语音打断 / ASR 后端回退：本轮已重新实现，mock 测试通过

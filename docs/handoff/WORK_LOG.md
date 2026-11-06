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
# 本机项目路径已脱敏，$python指向该项目虚拟环境解释器
& $python -X "pycache_prefix=$q\pycache" -m compileall -q app main.py tests
git diff --check
git diff --cached --check
```

`verify_final.py` 重放实际逐文件定向清单，完整展开命令、退出码和用时保存在 `commands.jsonl`；不是全量 CI。真实插件补测只对实际 `.js` 文件进行分类，不执行、下载或替换插件，不访问数据库。Qt 预览使用隔离示例数据和系统现有中文字体，不代表真实设备或业务服务验收。

**边界与收尾**：不在 GUI 解析技术日志或根据文本关键词判业务成功，不新增持久化通知数据库或语音任务队列；旧接口无确认时保留待确认。没有读取/写入 .coverage，没有提交、推送、打包、系统网络或真实游戏操作。实际麦克风、扬声器、网络服务与完整 Windows 缩放体验仍需用户验收。

**证据目录**：`%TEMP%\starlight-notifications-kfd4b5qn` 保存 before.json、original-paths.json、before-files、this-turn.diff、测试日志/XML、界面预览及最终指纹。新增文件在开发过程中也可能有中间备份；施工前是否存在以 before.json/original-paths.json 为准。以本轮增量 diff 区分已有改动，不把整个 HEAD diff 都算成本轮成果。

**最终真实结果**：`acceptance-final` 合并回归 942 passed、0 failed、1 skipped、0 warnings，pytest 65.93s，进程退出码 0；`acceptance-final.xml` 为实际最终源码对应结果，完整参数在 `commands.jsonl`。新增用例为通知模型 28、通知 UI 15、业务接入 17，共 60。原跳过项为隔离目录无真实插件文件；`run_real_plugin_check.py` 针对实际目录的原断言另行 1 passed（0.51s），不是插件联网验证。过程中的 941/937/936 等结果包含重叠，不能累加。

最终布局相关 47 项通过；Qt 组件 280/480/960/1440 宽度测试、360/960 实际预览及 1/1.25/1.5 离屏缩放通过。实际 QMainWindow 状态栏内胶囊高度为 44px，未被压缩。`compileall -q app main.py tests` 的缓存写仓库外，退出码 0；`git diff --check` 和 `git diff --cached --check` 通过，Git 另有部分文件 CRLF 转换提示，不是 pytest warning。

最终工作区：26 tracked 修改、44 未跟踪文件，暂存区空、HEAD 不变。本轮实际修改 18 份源码、7 份测试、6 份维护文档；原有未跟踪文件无缺失、未参与本轮文件无额外变化、测试副本源码及测试指纹与工作区一致。旧安装包、数据库和备份原样保留。至此停止施工，等待源码实机验收。

## 2026-11-05 识曲与语音日常使用收尾

用户授权自主进行必要的本地体验优化。本轮选择有证据的识曲门槛、重试/临时文件问题，以及语音执行结果不可见和设备失败状态，未扩大到播放器、语义规则或游戏架构重写。

**开始状态**：`a0-cleanup` / `e686786dd64c6fe5243c28377a0fbd39900ede47`；8 tracked 修改、31 未跟踪文件，暂存区空。先保存状态/文件指纹、已有修改备份，再在临时 HEAD 副本中叠加当前工作区测试；不使用真实数据库、用户配置或音频。

**精确修改范围**：

- `app/music/humming.py`：复用采集方法，空库在线识曲不做本地查询。
- `app/ui/dialogs/identify_dialog.py`：去掉空库阻断、接通重试、唯一临时文件、删除危险的晚到 finished 清理、上传说明与真实播放反馈。
- `app/ui/panels/music_panel.py`：识曲播放适配器传回 False/原始结果，不吞掉失败或把 None 当成功。
- `app/voice/runtime.py`：最近交互的线程安全内存快照、业务反馈独立于 TTS；麦克风失败释放、正确开关、关闭事件及单采集线程保护；ASR 空文本/处理异常提示。
- `app/ui/panels/status_bar.py` 和新增 `app/ui/dialogs/voice_feedback_dialog.py`：复用 GUI 一秒刷新，显示最近交互，不增加主 TAB，不执行新命令，不保存转写历史。
- `tests/ui/test_humming.py`：两处旧文案/空库前置约定更新，保留打开不采集、不自动建库的安全断言。
- 新增 `tests/ui/test_identify_readiness.py`（16）、`tests/ui/test_voice_feedback_view.py`（3）、`tests/unit/test_voice_activity.py`（11）、`tests/unit/test_voice_input_recovery.py`（6），共 36 个用例。
- 本目录 CURRENT_STATE、NEXT_STEPS、KNOWN_ISSUES、WORK_LOG 四份记录。

**真实测试证据**：

- 最初测试夹具误用本环境未安装的 qtbot，出现 7 个 setup errors；已改用现有 QApplication/Qt 事件循环，没有安装依赖或弱化业务断言。
- 修复前识曲 7 failed（空库阻断、重试无效、固定文件名、无所有权清理、错误播放文案、缺上传说明、无空库采集路径）；麦克风六个场景 6 failed、1 warning（包含后台未捕获设备异常）；识曲播放反馈 4 failed、1 passed、7 deselected。
- 过程回归：69 passed；设备与可见反馈 30 passed；语音相关 496 passed；UI 119 passed。过程结果存在重叠，不累加为总数。
- 最终 `run_logged.py final-regression`：727 passed、0 failed、0 skipped、0 warnings，pytest 41.20s，退出码 0。范围为 voice/tts/humming/shazam 与相关音乐语音单元测试，八份 UI 测试，以及 online_search_reliability、music_queue_snapshot、music_next_play；完整逐文件参数在下方 commands.jsonl。
- 隔离副本执行 Python `-X pycache_prefix=<临时目录>\pycache -m compileall -q app main.py tests`，退出码 0；`git diff --check`、`git diff --cached --check` 退出码 0。没有触碰 .coverage。
- 实际 Qt/QThread 测试覆盖主线程渲染、窗口复用、空库采集→在线替身→结果处理、迟到的 finished 不删除下一次文件。离屏窗口截图已检查；测试渲染单独加载系统现有中文字体，未改应用字体/系统设置。

**证据目录**：`%TEMP%\starlight-usability-d4egjzkz`，包含 before.json、before-files、this-turn.diff、commands.jsonl、final-regression.log、identify-preview.png、voice-preview.png。原文件与测试缓存均在仓库外保护；测试副本与最终修改文件哈希需核对。

**停止点**：只完成本轮源码与定向验证；未进行真实麦克风/声音/在线识曲/游戏验收。没有 Git 暂存、commit、push、打包或系统网络调整。不得宣称已发布或所有环境全部稳定。

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

## 2026-11-05：统一日间／夜间主题

- 范围：集中语义色、QPalette／QSS／Tokens、主题管理器；顶栏与设置入口及偏好保存；通知、灵动岛、队列、电影控制／字幕菜单、LoL 浮窗和状态视觉同步。完整文件列表见 [主题验收记录](../日间夜间主题验收.md)。
- 不重建业务服务或页面，不修改音乐／MPV／计时／ASR／TTS 算法。保留原有用户配色及媒体画面。主题偏好写入沿用原有配置目录，仅合并主题字段。
- 验证：新增主题 46 passed；业务保护 257 passed；混合 Qt 组合 219 passed、4 项既有失败；歌词基线对照 73 passed、8 failed。编译通过。未用替身测试冒充麦克风、声音或游戏验收。
- 处理了原型图标引擎及 StyleChange 生命周期问题，改用 Qt 原生图标并在主题切换时更新现有控件。最终混合 Qt 组合没有原生崩溃。
- 两套主题均已生成并检查实际 Qt 离屏截图（100%／125%／150%）。没有提交、推送、打包、清理或重置，等待用户源码实机验收。


## 2026-11-05 阶段性娱乐中心升级（本批收尾）

- 授权范围：音乐发现/队列拖动、电影本地首页/公开榜单、LoL 工作台、双主题与通知复用；禁止提交、推送、打包、清理。
- 起点 a0-cleanup / a338d2718db4ba3068b87dbb8fef94071afd9273，35 tracked 修改、39 个逐文件未跟踪项；终点 49 tracked、52 untracked，暂存区始终为空。历史修改和用户文件均保留；先保全源码，再用仓库外镜像测试。
- 本批增量 19 源码、10 测试、6 维护文档。完整清单及调用路径见 [阶段性娱乐中心升级验收](../阶段性娱乐中心升级验收.md)。没有重写随机算法、MPV、语音或 LoL 计时。
- 新增 6 测试文件/64 个参数化测试条目。最终不重叠集合：331 单元 + 186 Qt/主题 + 11 真实 MPV = 528 passed、0 failed、0 skipped、0 warnings。3 项文案/刷新复核另通过但不重复累计。编译、JS 语法及两个 Git diff 检查通过。
- 真实联网：B站生产服务电影100/剧集100/动漫99；Node现有插件酷狗56榜/开心汽水4榜，其余5插件超时。酷狗推荐分类15秒超时，不能算实际歌单联网通过。四个其他影视平台仅官方入口，自动聚合尚未接入。
- Qt混跑曾堆损坏/AV；旧测试确实遗留5440控件，补资源释放后对应窗口为0。仅清理不足以消除晚主题初始化崩溃；测试按 main.py 的早期主题初始化顺序后同组186项通过。完整首次失败证据保留，原生内存破坏的精确位置尚未证实，未修改ThemeManager掩盖错误。
- 截图实际覆盖两主题、800/1180逻辑宽度、100/125/150% Qt缩放，使用隔离示例数据。没有真实麦克风、听音、游戏或新安装版验收。完成本批后停止，等待用户简短源码验收。


## 2026-11-05 音乐与电影首页体验升级（本批）

- 起点49 tracked修改/52逐文件untracked，a0-cleanup / a338d2718db4ba3068b87dbb8fef94071afd9273，暂存区为空。终点50/56，仍为空；保护全部既有成果。没有提交、推送、打包、清理或配置重置。
- 7源码/4测试/6维护文档增量：电影分类预览+内部分类页、紧凑本地空状态、来源与官网区分；音乐六区发现台、按能力浏览/分页/有限缓存、搜索结果分离；工具收纳与共用卡片。完整清单见 [首页验收](../音乐电影首页体验升级验收.md)。
- 2新测试文件32个参数化条目。实际最终集合280单元+213Qt+3原生MPV+29片源+2接线/绘制复核（含1重叠），去重526 passed、0 failed、0 skipped、0 warnings。编译、JS语法与差异检查通过。
- 首次绘制枚举和旧入口断言失败均已修正，未降业务断言；末次审查复现并修正“未接综艺误报成功”和“筛选后旧操作目标残留”，原失败日志保留。
- 本次真实联网：13:54 B站电影100/电视剧100/动漫99，首页18预览、3请求，电视剧完整缓存100不增加请求，Qt实际海报异步解码成功。13:56音乐7插件加载、6返回235榜、1超时；酷狗96公开分类/首批30歌单成功。无听音/歌曲下载/录音上传。
- 实际离屏截图覆盖双主题、100/125/150%Qt缩放、800/1180宽及空库/分类页；不是物理显示器/听音验收。用户最少检查音乐导航不停歌、电影分类与本地续播字幕、主题和缩放。完成后停止，不自动展开其他施工。
- 全量本批diff/源指纹/日志位于%TEMP%\starlight-home-layout-pktneflh。


## 2026-11-05 公开榜单扩充与发现可靠性（本次实际施工）

- 在现有工作区（a338d271、50 tracked修改+56逐文件未跟踪、暂存为空）上继续增量施工，保护快照位于外部临时目录 starlight-sources-health-803f2nyb。
- 新增爱奇艺四分类公开SSR排名与腾讯四分类官方热搜；本轮真实链路得到B站299、爱奇艺100、腾讯200条记录，共599条，非599部去重作品。优酷/芒果仍只官网入口。
- 修复榜单旧读覆盖新缓存、Node榜单目录/详情未知结果假空成功、音乐曲目页刷新重复追加；五个失败探针均转通过。
- 原生异常在施工前副本同组合复现；仅对齐两个旧测试fixture的早期主题初始化，相同组合262 passed，保留首次AV日志与边界。
- 最终去重380个定向/Qt回归用例通过（含39新增），0 failed/skipped/warnings；三组最终执行76、79、262，有重叠不累加。9个Python编译、2个JS语法及diff检查通过。
- 当前插件真实目录6源235榜，1源超时；酷狗分类、歌单、歌手、专辑、TOP500曲目实读通过，仅元数据，不等于听音验收。48张两主题三DPR离屏图通过抽查；仍需用户短实机验收。
- 完整文件、命令、联网时间、失败和保护说明见[本批验收](../公开榜单扩充与发现可靠性验收_20261105.md)。未commit/push/打包/改VPN/改用户数据库。


## 2026-11-05 项目 Skills 与产品维护机制

- 新建根 AGENTS.md、.agents/skills 下五个标准 SKILL.md 和 PRODUCT_ROADMAP.md；规范按职责拆分，按需读取，不记录固定 HEAD/文件数量/临时测试结论。保留现有代码、测试、用户配置和构建产物。
- 本轮只读核查：核心95 passed、Qt79 passed；单独复核旧海报断言2 failed，未改或跳过。新歌错误提示与连续401状态通过外部内存探针复现，只记录后续方向。
- 技能标准校验、链接/源码路径检查和 Git 增量审查留在外部 starlight-project-skills-ga1r0ymr；没有提交、推送或构建。

## 2026-11-05 音乐反馈与电影交互测试收尾

- 施工前保护53 tracked修改、69逐文件未跟踪，HEAD a338d271、index空；外部快照与完整增量diff：starlight-music-feedback-e5g8hbse。
- 音乐发现区分明确不支持、超时、插件异常、成功空目录及部分结果；桥接getTopLists不再丢失错误类别，整体失败后的刷新不重复提交。未改普通搜索/播放算法、用户插件和数据库。
- 两条旧海报断言更新为默认首页→按钮/回车实际搜索→海报结果，保留数量/占位/tooltip及下载参数，补充不自动播放与一次搜索断言；测试按真实接口初始化并清理自有窗口/任务。未修改电影生产代码。
- 修复前首批19 failed/4 passed，桥接与刷新补充5 failed/3 passed；开发中另保留错误归属及在途重试状态两项首败并修复。最终79/156/86三个回归组，重复22项，去重299 passed、0 failed/skipped/warnings；新增35项，不是全量CI或真实网络/设备验收。
- 5个Python编译、差异检查及两主题离屏提示检查通过；未出现新原生异常/挂起，不宣称历史140秒Qt挂起已修复。停止施工，无commit/push/构建。

## 2026-11-05 海报加载与音乐停止调查

- 施工前54 tracked/71逐文件untracked、index空，a0-cleanup/a338d271；保护快照与完整增量diff在外部starlight-posters-playback-qmby3ifn。
- B站142条、腾讯已取得的150条中30条图片被HTTPS-only规则丢弃：同原始响应验证后仅升级可信CDN相同路径为HTTPS。爱奇艺100条原有图片字段完整；腾讯电影有界补读仍超时。
- 原加载失败永久缓存改为30秒后可刷新重试，成功24小时/256缩略图缓存/8并发/关闭清理；统一电影缺图占位，未添加假图或网络代理。实测部分图片仍8秒超时，不能宣称全部加载通过。
- 音乐现场无法补回结束原因与队列历史；仅增加脱敏的end-file、模式/大小/索引诊断，不改播放算法。隔离生成音频/真实MPV的3项EOF与模式检查通过，无真实扬声器听音。
- 海报首败8 failed/15 passed，开发中重定向1失败已修；原生探针ID/filename初始化错误留证并修正；组合357 passed/1旧HTTP拒绝契约失败，安全升级契约收尾34通过、音乐诊断6通过，最终去重359 passed、0 failed/skipped/warnings。新4测试文件33项，旧安全测试另加1反例。
- 无新原生崩溃/挂起；未commit/push/打包，未改用户数据库/配置/媒体/VPN。详细数量、样本、时间线与实机边界见[本批报告](../海报加载与音乐停止调查_20261105.md)。


## 2026-11-05 阶段性源码版本整理与发布验证准备

- 用户明确授权版本、README/CHANGELOG、About/Topics、精确commit/push及Windows CI；不构建安装包、不修改网络/仓库安全设置。
- 施工前56 tracked/76逐文件未跟踪、index空，起点a338d27。在工作区外保存原始diff/文件清单/哈希与快照，从HEAD+精确候选生成测试副本，排除旧安装脚本和私人数据。
- 新增VERSION=0.1.1.dev1和Python读取入口；配置兼容不重写个人设置；构建指纹与冻结数据、Inno模板读取同源版本。README重写、CHANGELOG和成果审查补齐，历史私人机器路径脱敏。
- 本地完整单元2593 passed/9 skipped/1 warning；版本打包37 passed包含其中。Qt首轮408 passed/1 failed，旧测试Track无album，改用OnlineTrack保留旧结果和快照断言；补测曾发生0xc0000374，有限详细诊断128 passed。合并最终测试条目去重436 passed，不抹去原生首败。真实隔离MPV集成4 passed（生成媒体、null输出，非设备验收）。
- 测试日志/JUnit/命令/退出码/首败与发布清单保留在外部starlight-release-archive-x3scmhre；不写入仓库。Windows CI和远端同步结果由最终提交SHA交付报告记录，不预写成功。


## 2026-11-06 音乐搜索桥接可靠性

已修复串行全源搜索/别名重复/单插件挂起、JSON结构导致reader退出、进程代次与写入/刷新竞态、关闭等待及重启预算未计数。19项新增隔离Node用例，最终194 passed/0 failed/0 skipped/0 warnings，收尾19项与总数重叠不累计；首败保留。真实外网音源及Windows退出仍待自然使用，Promise超时不代表取消已发HTTP。海报/Qt安全快照/播放队列不改，不查系统网络，不commit/push/build。
详见[本轮验收](../音乐搜索桥接可靠性验收_20261106.md)。


## 2026-11-06 ASR测试替身初始化与CI收尾

07585a467294ae41abc2de905ddac96201a367d6的首次Windows CI（42165707510）为2608 passed、4 failed、9 skipped、1 warning，无原生异常标记。四项关闭测试绕过JSMusicSource构造函数，缺少_lock和_pending；本轮在独立候选树复现4 failed。

仅维护test_asr_status_propagation.py：改用真实构造函数，隔离_start_bridge并完成ready，不启动Node；fixture收尾执行真实close。原有测试和业务断言全部保留，新增一项对照真实构造实例的字段、类型、独立状态与锁能力测试，避免手写替身字段再次漂移。音乐桥接生产代码、VERSION及其他历史成果未修改。

候选树运行6份ASR测试及6份音乐桥接/搜索回归：139 passed、0 failed、1 skipped、0 warnings，退出0。原四项及新增测试均包含其中，不重复累计。跳过为既有真实插件文件断言，隔离目录未复制用户插件。compileall及diff检查结果、精确提交和新SHA的Windows CI以本轮实际验证记录/最终报告为准；保留首次失败，不预写新CI成功。


## 2026-11-06 工程质量升级（未发布）

原unit工作流字节保持不变；新增独立Windows UI smoke工作流与有界进程执行器，选择主题25、通知15、电影卡片39、队列31共110项。当前工作区及已提交基线各组独立3轮通过，定稿CI入口再3轮110项通过；快照12项本地3轮通过，但其生产依赖尚未入库，本轮不放入CI、不改GC修复。

本轮覆盖122个唯一UI节点，1356次UI执行仅用于有限稳定性重复，不算新增覆盖。新增21项CI工具测试最终全通过，与UI共143个唯一节点；失败/跳过/pytest警告均0，未发现原生异常标记。原始日志、JUnit、Ruff扫描、快照和增量diff外置保留。定稿Ruff新增文件0诊断，compileall/diff检查完成后以实际收尾记录为准；未运行真实GitHub CI或设备验收。

Ruff 0.17.4仅开发工具：无配置首次2321条/390文件，新显式E4/E7/E9/F规则口径1105条/246文件，不冒充修复历史违规；只清理新增测试的1条未使用导入，不全库格式化、不引入运行依赖。四个大文件只审计，未发现目标静态导入环，记录职责/测试边界与小步拆分顺序。两处条件路径未定义名称（combo_panel._ACCENT_Q、main日志异常分支logger）保留为独立修复候选，不顺手改业务。

许可证核查未替用户选LICENSE：mutagen GPL、Qt/LGPL等需核对发行方式；AudioFP/pychorus、模型/构建、图标插画来源待补，Lucide注释MIT/ISC不一致已记录。README尚无可复用产品截图，仅交付六类展示与隐私方案，不上传私人界面或虚构榜单截图。不commit/push/build，不碰VPN和用户数据。详见[本轮报告](../工程质量升级验收_20261106.md)。

收尾发现根目录txt忽略规则会隐藏requirements-dev.txt，.gitignore仅增加该文件精确例外，未放开用户日志/配置。

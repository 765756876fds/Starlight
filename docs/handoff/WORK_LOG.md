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


## 2026-11-06 工程质量第二阶段：两个条件分支已修复

确认HeroListDelegate焦点且选中分支的_ACCENT_Q确实未定义，改为复用同次绘制中由Colors.ACCENT创建的accent；不新增常量、不改主题/其他绘制分支。main.py的应用logger是在日志初始化之后才赋值，错误处理会先遇到NameError；仅将该分支改为logging.getLogger(__name__).error，不提前创建文件Logger，正常配置及后续get_logger单例保持原样。

新增12项真实调用回归：8项Qt绘制矩阵覆盖日夜主题、选中/焦点组合、实际描边颜色和QPainter状态恢复；4项隔离子进程导入真实main.py副本，覆盖makedirs、滚动FileHandler、basicConfig失败与正常日志落盘。只注入子进程内I/O故障，未启动应用服务、游戏或键盘hook。最终同测试对照修复前5 failed/7 passed，修复后12 passed；第一次修复后曾有2项采样点断言失败，实际像素探针确认Qt矩形右边界后修正坐标，保留强调色断言与全部首败日志。

本轮最终定向回归96 passed、0 failed、0 skipped、0 warnings，退出0，包含新增12项，不与单独验证重复累计；未发现实际原生异常，compileall通过。Ruff新文件0诊断；main.py历史33→32、combo_panel.py历史2→1，均只消除对应F821，其他诊断不扩散整改。

只读复核UI工作流：windows-latest/Python3.11/offscreen，4组110项、单组120秒/Job15分钟，仅失败上传pytest.log/JUnit/result.json，保留7天，contents只读。工作流、执行器、21项工具测试、原unit工作流均未修改；上一阶段三轮是本地隔离结果，本轮无GitHub执行或远端通过结论。

许可证待决清单继续保留：自有代码许可由用户决定，mutagen GPL及Qt/LGPL需核对发行策略，AudioFP/pychorus、模型/原生构建和图标插画的来源/授权仍待补，Lucide声明冲突待单独处理；没有添加MIT/Apache/GPL或宣布授权完成。原有未提交成果、数据库/备份/安装包/转储保留，不commit/push/build，不碰VPN。外部证据starlight-quality-phase2-*保留基线、首败、像素探针、JUnit、Ruff比较和本轮增量diff；完成后停止。


## 2026-11-06 自主协议下的工程质量归档批次

用户长期授权经审阅的精确commit/push/CI，并补充增量上下文、分层测试及完成即停止规则；本批只交付两项目标：已验证的条件分支修复归档、独立UI smoke首次远端验证。协作规范只记录新的授权/额度规则，不带入其他未审阅历史变更。

候选源码和测试与上轮验收哈希一致；从HEAD构造不含其他历史补丁的候选副本，补测新增条件回归及CI工具共33 passed、0 failed、0 skipped、0 warnings，退出0。复用既有96项回归及独立UI组本地三轮证据，不重跑全套。原unit工作流不改；GC/影音等其他历史修改不混入。本次最终提交SHA、unit/UI远端结果以实际交付记录为准，不提前填写成功。VPN暂停、许可证待决、用户数据保护及不构建安装包等边界保持。


## 2026-11-06 电影控制器单边界抽取

施工前a0-cleanup/03acf8838eaed4e5b11640db4c2cdcd90a74af33，20 tracked修改、39未跟踪、暂存为空；控制器原先无差异。外部备份相关文件并保存全部已有未提交文件哈希，未读写.coverage。完成职责/调用方地图后，只抽取四个CMS/分集纯函数至cms_parser，旧导出保持同一对象；其余方法AST不变，不改音乐、LoL、语音/TTS、GC、VPN或任何用户配置。

修改前66 passed；首次相关组合204 passed/1 failed，新测试的导入观测器误拦截Python加载器_io。保留首败，仅在拦截前读取/编译源码，继续严格限制纯模块只导入json。从HEAD构造不混入旧补丁的最终候选：205 passed、0 failed、0 skipped、0 warnings，退出0，含新增20项，不重复累计。compileall退出0，新文件Ruff退出0；控制器既有F841前后1项，退出1，未扩大修复。相关电影Qt、海报、字幕和续播回归均在该组合内，无原生异常。

本轮没有真实外网/私人媒体/Windows实机测试，不新增运行依赖或后台服务。维护记录仅追加，提交时只选本轮增量；完整命令/JUnit/首败/AST与哈希证据保留外部starlight-movie-extract-*目录。审计报告记录剩余耦合和停止点；完成本批后不启动下一个拆分。


## 2026-11-06 UI CI 成功报告可观测性

完成最小报告接入：原四组执行器、进程隔离、120秒上限和首败即停不变；新增report_ui_smoke脚本，成功/失败均写Step Summary并上传独立JUnit、退出信息及简短摘要。导出剥离主机/属性/日志/失败正文，仅窄白名单，不传dump/音频/用户配置/数据库；原始诊断留执行目录。缺失/损坏报告是report_error，未执行为not_run，未记录退出的harness为reported，不猜成功或警告数。原Unit工作流未改。

新增报告20项，与原helper共41项通过；四个真实Qt进程110项通过，导出CLI精确读出25/15/39/31，零失败/跳过/警告。最终报告错误分支补强后再验41项，不重复累计。compileall和新增文件Ruff通过；首次远端上传/摘要效果待实际新SHA验证，不能用本地结果替代。


## 2026-11-06 ongoing 服务纯玩家转换边界

职责审计见docs/CI可观测性与LoL玩家转换验收_20261106.md。仅抽取_to_player执行语句至player_conversion.to_current_player，旧静态入口和self._to_player可覆盖调用保留；其余21个服务方法AST不变。不改LCU、身份合并、代次缓存、轮询/退出、技能计时或提醒。现有stop两秒join不代表所有慢任务已退出，作为未专项复现的既有边界记录，不顺手修生命周期。

本地去重237节点通过：41 helper、74 LoL单元、110原四组Qt、12其他LoL Qt；115项定向组合与28项LoL Qt/转换验证有16项重叠，不累计。修改前79项基线通过；本轮pytest零失败/跳过/警告、无原生异常；compileall通过，service既有9项Ruff诊断前后不变（7 E402、2 E701），其余本轮文件零诊断。全部项目外HEAD候选隔离运行，无真实LCU/游戏/外网业务验收。原20个tracked修改、39个未跟踪文件及历史文档字节保留；只归档本轮增量，不build、不碰VPN，完成两个目标后停止。


## 2026-11-06 任务04：音乐刷新可靠性与电影返回位置

本批只处理两个有价值问题：MusicDiscovery刷新音源时旧目录被作废而新请求被running保护跳过，以及MoviePanel海报搜索返回会重置分类。前者复用LatestTask只排最后一次新读取，并处理隐藏页面的延后更新；后者复用现有返回按钮/同一页面实例，保留分类、筛选、排序、选中项与滚动，普通搜索仍可回首页，返回后不继续显示搜索中。无播放器/队列状态机/电影控制器/LoL/语音/GC/网络设置修改。

新17项修改前13 failed/4 passed，修复后17 passed；100/125/150%独立进程各17通过。相关验证去重397 passed、8 failed、0 skipped、0 warnings；8项旧歌词断言在原始HEAD同组同样失败（两者均104 passed/8 failed），没有跳过或修改歌词来绿化。最终新17+电影51共68通过，单元125通过，原CI四组本地实际命令110通过。新文件Ruff零违规，源码既有6项诊断前后不变。审计和完整证据见docs/音乐电影体验精修验收_20261106.md及项目外starlight-media-polish-*目录。

临时CI新组120秒、子进程取证50秒、125%首轮截图90秒超时均留证；阶段取证/串行截图后来通过但根因未确认，不能宣称超时已解决。故未扩充CI分组，执行/报告脚本与两个工作流保持原样，新17项只算本地覆盖。三档双主题、660/1000逻辑宽度截图已检查，素材均为隔离测试数据，不是实网/实机验收。旧歌词测试契约和启动超时只记录，不增加第三项施工；音乐曲目返回列表便利性暂缓。原20个tracked、39未跟踪文件及文档原文保留；按最终实际远端核对与CI回执收口，不打包、不清理，完成后停止。


## 2026-11-06 LoL技能计时悬浮窗透明背景恢复

- 用户明确要求恢复既有基础功能；不启动LoL主动研发。Git历史确认c5cd641cea21c7b58cf4adc61914ffa6cb3a7e8b新增根窗口paintEvent，以230/255透明度铺满圆角背景；此前同一窗口已启用WA_TranslucentBackground且没有这层绘制。离屏像素对照：修改前日/夜背景alpha=230，恢复历史无底板绘制后均为0。
- 仅删除根窗口背景绘制及其专用QPen导入；原图标/倒计时绘制、置顶/无焦点属性、Windows分层属性、拖动/点击、100ms刷新及计时模型保持原样。更新旧主题测试的背景契约为真正透明，保留位置、尺寸、模型和定时器断言；新增13项真实Qt像素/前景/信号/窗口属性测试。
- 新测试修改前6 failed/7 passed，修改后13 passed；125%与150%独立进程各13 passed（重复缩放验证不累计为新增测试）。仅本轮三文件叠加HEAD的干净候选45 passed。相关六文件回归修改前后均146 passed/2 failed：test_glass_theme中的test_minimize_and_close_slots、test_boot_timeout_forces_switch为既有失败，保留首次日志及对照，不修改、不跳过。无skip/warning或原生崩溃记录。新13与既有148去重为159 passed/2项基线失败，45项和DPI复核不重复累计。
- 实际命令由项目外qt_entry.py先创建QApplication和ThemeManager，再调用pytest；参数为-o addopts= -p no:cacheprovider --basetemp <外部临时目录> --junitxml <外部报告> -q。定向tests/ui/test_overlay_transparency.py；回归tests/ui/{test_theme_switching,test_overlay_incremental,test_glass_theme}.py及tests/unit/{test_overlay,test_theme_palette,test_single_instance}.py；干净候选仅运行前三文件中theme_switching、overlay_incremental与新增transparency。编译及git diff --check通过；新测试和源码Ruff零违规，旧theme_switching的1项F401修改前后相同。完整命令、JUnit、首败和历史差异保留于项目外starlight-overlay-alpha-*目录。
- 未改LCU、技能计时、提醒、主题/GC实现、其他模块或系统设置，无新依赖/常驻任务。离屏alpha、可见倒计时和真实点击信号不等于游戏内DWM验收；用户只需在实际游戏中确认背景透出画面、图标数字清楚并可按原方式点击/拖动。旧Qt原生异常不能据此宣称永久消失。


## 2026-11-14 Voice Semantics v1

明确规则优先，仅增加音乐暂停／继续／下一首／上一首的有限语义候选。新增对象在前等明确表达，复用 CommandType、Runtime、CommandBus；原始问号／引用在归一化前保护。没有可靠原子当前媒体状态接口，无对象自然表达先提示明确音乐；旧专用“暂停／继续”全局契约保留。不改 ASR、VAD、TTS、调度、电影、LoL、MPV、队列或安装包，不引入依赖／常驻任务。

接入前探针36 failed/199 passed，接入后同组235 passed；最终669单元＋18 Qt通过，无失败／跳过／警告，其中105项新增。干净HEAD精确叠加三份本轮源码／测试257 passed，和前述重叠不累计。最终178条独立影子文本：正向6/20→20/20，不应执行候选9/158→0/158；不代表真实麦克风识别率。编译通过，无原生异常标记。Ruff模块未安装，项目外官方工具下载超时，未完成该检查，不宣称通过。真实麦克风与设备播放仍待验收。

详见[本轮设计与验收](../Voice_Semantics_v1_20261114.md)。其他历史成果、用户数据、模型、安装包与转储保留；维护记录仅追加本段。提交／同步／新SHA对应Windows CI以最终回执为准，不借用旧结果。本轮完成后停止，不开展v2。


## 2026-11-15 音乐本地偏好与推荐第一阶段

- 新增独立 music_preferences.db（原音乐库同目录），保留原历史/手工歌单；幂等初始化、未来版本和损坏/不可写降级，不保存录音、凭据或播放URL。
- 接通有限真实榜单选择、明确偏好/弱历史/排名加权随机、最多3榜/4候选、45秒总预算、既有线程池和取消代次；推荐自动播放不参与推测强化。
- 语音“我想听歌了”等走现有异步CommandBus；“换一个”消费真实推荐集合，电影正在播放或推荐上下文失效时不猜测对象。
- 发现页加入为我播放、换个榜单、喜欢歌手、减少这首，真实来源/理由回传GUI和通知中心。未改MPV、队列算法、LoL、电影逻辑或网络设置。
- 定向及相关单元：246 passed；Qt：69 passed；隔离真实MPV生成音频三模式：3 passed。三个集合节点不重叠，共318 passed，0 failed / skipped / warnings；后续干净候选和CI另行记录，不重复累计。
- 原基线6条核心语句契约6 failed；修复后覆盖在新单元测试内通过。开发首败包括新测试的取消结果分类与超时计时范围，保留原日志；已修正取消分类，超时测试预先初始化SQLite，仍保留<0.5秒等待断言。
- 有限真实联网：开心汽水4个榜单入口，详情0首（插件track_ranks异常）；其余6个音源目录超时。没有确认真实网络歌曲可播，不将生成音频验收冒充真实平台或麦克风验收。
- Ruff新文件零违规；本轮修改旧文件原有13项违规保留并做基线对照；编译和差异检查执行结果见阶段验收。未升级依赖/版本、不打包。
- 用户验收：音源可用时说“我想听歌了”→“换一个”；喜欢歌手后重新推荐；重启偏好保留；不可用时看到真实失败并可重试。当前网络来源不可用仍是外部限制。
- 详见 docs/Music_Recommendation_Stage1_20261115.md。仅此阶段增量纳入归档，其他历史文件保留。

- 干净候选真实MPV首次发现随机起点被重复消费（1 failed、2 passed），已保留首败；仅为新推荐集合增加play_at(collection_start=True)起点对齐，保留其他随机顺序及旧接口默认行为。正序/倒序两种确定性回归均通过；涉及队列的定向回归135 passed（与前述单元集合有重叠，不累计）。最终干净候选和CI另行核验。

- 最终干净候选：受影响队列/推荐135 passed；新增单元完整44 passed（两批重叠，不累加）；Qt35 passed；真实MPV三模式3 passed。工作区Qt较宽回归69 passed。新文件Ruff通过，旧文件无新增违规，compileall及两类diff检查通过。保留全部67个施工前文件指纹；15个文件仅本阶段增量进入候选。

### 音乐推荐收尾：取消与电影暂停确认时序

实际控制器探针复现：推荐在等待电影暂停确认期间被取消，旧 loadfile 仍执行（1 failed，日志在项目外隔离取证目录）。在确认返回后、loadfile 之前补充仅推荐路径的纯代次检查；旧 before_load 钩子仍只调用一次。新增测试验证结果为 UNKNOWN、未加载旧歌曲、未建立推荐会话。定向回归 69 passed、0 failed/skip/warnings；退出码0。此补丁不修改电影播放器、MPV、既有队列语义或网络配置。

### 发布验证首败及资源收口

首个 Windows Unit CI（42f6c84，43624479304）结果：2840 passed、3 failed、9 skipped、1 warning。两项在线队列测试在后台 loadfile 调用后立即读取历史，早于实际写入；改为有界等待实际历史后保留原全部断言。命令注册测试的 SimpleNamespace 补充两个新增音乐方法。对应两文件 165 passed、0 failed/skip/warnings。

另以真实SQLite连接保留引用验证：原偏好库仅使用事务上下文，成功/异常后连接均未关闭（2 failed）。补充 closing，确保事务提交/回滚后立即释放连接；新增两参数用例，推荐单元完整47 passed、0 failed/skip/warnings。取消补丁的干净候选含真实MPV三模式回归72 passed（与其他批次重叠，不累计）。旧测试等待修正不会更改业务断言。最终SHA的Unit/UI结果另行记录。

### Windows Unit CI 旧初始化竞态收尾

1e4f8dc 的 Unit CI（43625684136）：2845 passed、1 failed、9 skipped、1 warning；原三项失败已修正。唯一新失败为 test_same_puuid_no_surge_no_reload：计数从0变10，属于首批历史尚未加载，非重复请求。隔离施加150ms初始加载延迟时，原始de0基线与新候选均1 failed，证明为既有测试初始化竞态。仅修正测试等待首批十人历史全部完成后再记录计数，保留后续计数严格不变断言，并参数化即时/延迟初始加载；整文件11 passed、0 failed/skip/warnings，未修改任何LoL生产代码。最终提交的CI结果另记；未对同一SHA反复重跑。

### 第二阶段：影视偏好推荐与真实媒体情境（2026-11-15）

用户已反馈音乐第一阶段六项真实在线功能验收通过（用户验收记录，本轮未重新操作其设备）。本阶段复用公开榜单、旧电影搜索/窗口/MPV与现有CommandBus，新增独立movie_preferences.db、最多6条推荐、明确喜欢/不感兴趣、可确认的电视剧/动漫续播与音乐/电影情境控制。没有LLM、运行依赖或常驻服务，不修改音乐推荐/队列、LoL、网络与安装器。

有限联网：电视剧B站100/腾讯50/爱奇艺25，共175条，65条真实评分、0标签/年份；真实数据产生6个候选。电影三个来源本次超时；豆瓣悬疑无返回，不伪造高分/类型筛选。未知元数据保持未知。

基线249 passed；8条新增核心表达隔离对照从0/8到8/8。开发中发现并验证事务原子性、重复建连、旧请求隔离及旧条目身份契约；首败均保存在项目外目录。新增功能单元63 passed、相关Qt167 passed、真实隔离MPV1 passed曾取得，最终候选统计另追加，不累计重叠批次。截图仅测试数据，两主题/窄窗口/100-150%离屏缩放，不代替实机或真实联网。
详见 docs/Film_Recommendation_Context_Stage2_20261115.md。只归档本阶段增量，原电影URL历史补丁、音乐/语音/LoL等未提交成果与用户数据保留。当前等待最终干净候选、完整diff与Windows Unit/UI CI核验；本轮不打包、不进入第三阶段。

### 第二阶段最终干净候选验收

- 隔离副本仅包含本阶段23个暂存文件，排除历史未提交源码。定向单元714 passed、Qt 168 passed、原生MPV 4 passed；各组0 failed、0 skipped、0 warnings。三组范围不重叠，早期重叠批次不累计。
- 命令：`.venv/Scripts/python.exe -B -X faulthandler <外部证据目录>/run_tests.py <干净候选> <外部证据目录> <组名> [--qt] <测试文件>`；完整文件列表及JUnit分别见外部 clean-unit-final-command.json、clean-ui-final-command.json、clean-native-final-command.json。真实MPV使用生成视频及隔离配置，非麦克风或渲染实机验收。
- 新文件Ruff零违规；既有文件48项历史违规，本阶段零新增。编译和暂存/工作区差异检查通过；两主题、640/1100宽度、100/125/150%缩放共12张最终离屏截图，测试内容已明确标记。
- 用户实机仍需确认推荐选片、电视剧续播及音乐/电影情境目标；高分悬疑真实网络本次数据不足，不宣称联网播放成功。最终Windows CI另核对提交SHA及实际范围。

### 智能推荐第三阶段：兼容性联调（2026-11-15）

保留队列未定位观察项，未改播放算法；新增脱敏music_collection计数诊断与O(1)原子队列快照，日志失败不影响业务。新增13项跨媒体/数据库/诊断测试，并归档此前19项队列单元、3项真实MPV回归及调查记录。初步682项相关单元、140项Qt、7项真实生成媒体MPV均通过；最初不存在测试路径exit4及未配置FFmpeg的4项skip如实保留，配置后只补跑缺失4项。音源/播放器替身与真实MPV、用户已有联网验收分别记录；本轮不重复请求平台，不打包、不改网络、不优化LoL。详见docs/Recommendation_Stage3_Acceptance_20261115.md；最终干净候选和CI结果另行追加。

### 第三阶段最终干净候选验收

- 仅含本阶段12个精确文件的候选副本：clean-unit 682 passed、clean-ui 140 passed、clean-native 7 passed，各组0 failed/skipped/warnings、exit0。三组范围不重叠，共829个唯一用例；早期重复执行不累计。
- 8个Python文件compileall exit0；新文件Ruff零违规，既有文件4项历史违规、零新增。完整staged diff及敏感值检查通过，git diff/check和cached检查通过。
- 原70个变更/未跟踪文件均保留；66个非目标原文件哈希一致。控制器历史41行诊断保留在未暂存区；仅本轮生命周期日志增量入提交。前轮观察测试仅更新技术日志接口断言，业务断言不变。
- 此次只补充可观测性和验收，没有业务算法修复；推荐队列偶发空状态仍未定位。真实联网采用用户已有验收，不用替身或生成音频冒充在线/麦克风实机成功。最终Windows Unit/UI CI需核对新SHA，另附本地回执。

### 首次CI唯一失败：旧MPV测试调度契约（2026-11-15）

- 6e5918c首次Unit运行43683385735：1 failed、2942 passed、9 skipped、1条既有warning；UI运行43683385786成功（四组110 passed、0 failed/skipped/warnings；helper41 passed、1条既有warning）。首次完整日志已保留。
- 旧test_send_command_serialized错误假定线程1先start就先取得锁。外部控制线程2先执行，在父962b5f7和本轮6e5918c均复现request_id 2 != 1（各1 failed、exit1）。两提交的MPV源码和旧测试字节相同，非推荐或新增诊断回归。
- 只修复tests/unit/test_mpv_threadsafe.py：用Event控制两种先后顺序，模拟响应按实际发出的请求ID和属性生成；仍严格检查请求编号、属性响应不串线、最大未完成交换数为1、缓冲消耗完毕及两个线程均退出。播放器源码不改。
- 定向3 passed，连同诊断/联调共16 passed（13项与前682项重叠，不重复计数）。移除锁的外部负对照两种顺序均失败，证明未放宽并发安全断言。Ruff及编译通过；初始测试路径/FFmpeg环境记录仍保留。
- 本阶段本地唯一用例更新为832项：682相关单元、3项MPV测试、140项Qt、7项真实MPV，均0 failed/skipped/warnings。首次CI失败已按证据修测试，不无限重跑；最终提交需另验Unit和UI。此次仅测试/记录，UI工作流不匹配测试文件路径，将在确认最终SHA无既有运行后通过现有workflow_dispatch手动触发一次，不改YAML。

### 第二次CI唯一失败：播放模式测试替身与清理（2026-11-15）

- a9b32b9的Unit运行43684570663：1 failed、2943 passed、9 skipped、1条既有warning；已修正的MPV并发测试通过。该SHA的UI运行43684646925通过（四组110 passed、0 failed/skipped/warnings；helper41 passed、1条既有warning），四组独立JUnit已下载。
- 唯一失败test_manual_next_invalidates_poller：旧替身get_property默认返回MagicMock，idle-active被当成True，健康文件触发坏文件自动跳过并写入300ms防抖时间戳，干扰手动切歌断言。日志同时出现natural切歌、数据库已关闭后的历史写入及手动next防抖。不能仅称环境偶发。
- 用外部受控写入延迟和固定防抖时钟，在父962b5f7及阶段源码均复现同一generation未变化断言失败（各1 failed、exit1）。同一探针对修正后的替身1 passed；没有修改生产确认、高潮或切歌算法，也没有关闭安全守卫。
- 只修正tests/unit/test_playback_mode.py：提供False/数值等真实IPC类型，隔离CommandBus/EventBus/StateStore及LX连接；测试结束停止自身控制器、等待自身线程、关闭两条临时DB连接和自身事件总线。原业务断言全部保留，已关闭连接不重复close。
- 定向组合97 passed（含大量前轮重叠，不累计）；最终该文件15 passed、0 failed/skipped/warnings，Ruff及编译通过。各次CI、受控首败和测试清理记录均保留。
- 本阶段本地最终唯一范围847项：相关单元682+MPV并发3+播放模式15，Qt140，真实MPV7；各组0 failed/skipped/warnings，不能把重复执行或负对照失败混入成功数。最终新SHA再核对Unit/UI，保留推荐队列未定位观察项。本轮不打包、不改网络或业务架构。


### 自然媒体控制与语音响应性（2026-11-15）

对象省略的暂停/继续通过统一有限语法进入既有实时MediaContext，保留裸全局及明确领域/TTS安全优先级。修复GUI音量IPC与偏好入口等待播放锁；推荐在途读取最多2个；被动语音观察按需单线程、1执行中+1最新待送，命令不合并，新增分段计时和8秒旧输入拒绝。历史64047ms具体来源尚无证据，不能宣布已定位。947单元、165离屏Qt、独立60秒慢源1例、真实隔离MPV4例通过（去重1117，新增56）；不冒充外网或麦克风验收。纯HEAD混合Qt出现一次0xc0000374，完整保留；实际165项使用原样历史未提交GC/fixture保护，本轮不修改或混入，远端纯提交混合Qt风险仍需独立归档。旧队列观察项不改算法。详见docs/Voice_Control_Responsiveness_20261115.md。

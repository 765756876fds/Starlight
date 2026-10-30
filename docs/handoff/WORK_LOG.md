# Starlight 2.0 — 工作历史

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

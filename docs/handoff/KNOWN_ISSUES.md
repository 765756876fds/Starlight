# Starlight 2.0 — 已知问题清单

## 2026-11-05 统一通知批次的边界

- 已用确定性探针修复：重复音频留下多余处理中记录、播报提供者异常缺少可见附注、通知观察失败影响业务返回、旧 TTS 错误归给新请求、快速状态共用时间戳时胶囊漏更新，以及主窗口状态栏压缩两行文字。
- 不把 `None`、旧接口只受理请求或开始加载当成完成；部分操作会保守显示待确认，请以对应业务页面为准。本轮没有批量重写播放器协议返回值。
- 没有取消已发出的 HTTP、MPV 或其他业务操作。旧语音反馈按原代次检查，独立游戏提醒保留原有优先级及 TTL，不按当前胶囊指向取消。
- TTS 真实联网合成、实际扬声器、ASR 麦克风和 LoL 客户端尚需用户确认；没有切换 VPN、故障注入真实网络、打开键盘 hook 或真实自动化。
- Qt 离屏组件已按多个窗口宽度及 1 / 1.25 / 1.5 缩放检查。完整 Windows 多屏、高 DPI、真实设备体验仍未验收。
- 浅色调色板是未来接口，尚未提供主题切换；通知历史不跨重启保存。旧语音反馈对话框文件为已有未跟踪成果，原样保留，但不再由主界面创建。
- 隔离回归缺少真实插件目录时有一项原有文件断言跳过；该项另对实际插件目录只读运行通过。插件文件分类通过不等同真实音源搜索/播放通过。

## 2026-10-30 本次同步验证的未收口事项

- test_humming.py::test_runtime_dispatches_identify 使用仅带execute的SimpleNamespace；当前运行时检查has_handler后才分发。真实CommandBus已有该API，独立隔离验证识曲handler调用一次成功；这是仍保留的测试替身契约问题，本次不顺手修复。
- 本次暂存快照727 passed、2 failed，另一项为下方已知movie pauser未注入测试。不得把本次回归或未来CI写成全绿。
- Lite安装器辅助脚本仍使用旧dist目录约定，本次未纳入提交，不将其描述为已通过最新构建验证。所有安装器、模型、运行资源和私人插件继续保留在本机，不进入源码提交。
- 源码模式ASR健康接口、麦克风打开和电影暂停回调接线有新运行日志；实际听音、1.5倍速、连续语音和TTS过期反馈仍需用户确认，未完成冻结程序发行验收。

## 2026-10-30 两项修复后的验收边界

- 单引号完整引用（含跨行）不再执行；音乐倍速协议错误返回 False，无响应或不可信响应返回 None，不更新上次确认档位。代码级回归通过，真实 Windows MPV、麦克风和 TTS 待验收；旧安装包不包含修改。
- 本轮扩大范围发现 test_music.py::TestP0ConcurrencyAndRobustness::test_apply_stream_unifies_headers_and_state 缺少 movie pauser 注入，被现有安全守卫阻断。用施工前 controller.py 快照同样复现；main.py 实际存在 set_movie_pauser 接线，故未据此修改生产逻辑或无关测试。独立记录，不把 529 passed、1 failed 描述为全部通过。
- set_property 新增响应返回，其他调用方仍忽略响应；本轮仅消费音乐倍速的结果，没有将所有 MPV 控制命令改为三态确认。

## 2026-10-30 对抗性回归后的剩余边界

- 真实麦克风、中文 ASR 误识别、TTS 合成/播放、LCU 和键盘 hook 本轮未实测。
- 旧音乐搜索 None 仍混合“无结果/来源失败”；跳转、部分音量等旧接口仍缺完整协议确认，不能据此报成功。
- 代次只挡住未开始的旧 handler、部分电影搜索检查点及晚到/待播反馈；不撤销已发出的 HTTP、MPV 或已进入 TTS 的操作。音乐搜索内部与 movie.play 内部仍有不可取消区间。
- 反馈代次为整个语音会话共享：一条新的已识别指令（含查询）可使旧搜索反馈失效；手动 UI 操作没有统一加入该语音代次。不要宣称全应用操作可取消。
- “清空歌单”旧别名实际清队列、“下一部”实际下一集；VOICE/TTS 开关只有常量未见完整语音接线，本轮保留现状，见能力矩阵。
- 本机根目录日志/部分 pycache 权限受限，测试使用临时日志和外置 pycache；未改日志配置，也未触碰 .coverage。

## 2026-10-30 本轮边界

语义层回归通过，详见 `docs/语音语义层整治验收.md`。真实设备未验收；音乐搜索 None 原因不唯一；已发出的播放器操作不可由反馈代次撤回。测试机器 ./logs 文件权限导致部分独立测试初始化失败，隔离日志后通过，未改日志系统。下方条目保留为历史记录，未经核查不得套用为当前发布状态。

状态标签：CONFIRMED / REPORTED / FIXED_UNVERIFIED / TESTED / BLOCKED / CLOSED

## CONFIRMED（当前代码已证实）

### ISS-001：llama 后端资源缺失
- 症状：third_party/llama/vulkan/ 和 cpu/ 目录不存在。
- 证据：Test-Path 返回 False。
- 影响：build.spec binaries 引用的文件缺失，无法构建安装包。
- 状态：BLOCKED

### ISS-002：ASR 模型文件缺失
- 症状：models/*.gguf 不存在。
- 证据：Get-ChildItem 返回空。
- 影响：setup_full.iss 模型引用缺失。
- 状态：BLOCKED

## FIXED_UNVERIFIED（已修改，验证不充分）

### ISS-003：用户数据目录分离
- 症状：旧版写 Program Files，新版应写 LOCALAPPDATA。
- 修复：path_resolve.py 加 USER_DATA_DIR + migrate_legacy_user_data()。
- 验证：mock 测试通过，真机未验证。
- 状态：FIXED_UNVERIFIED

### ISS-004：ASR 双后端回退
- 修复：asr_bootstrap.py vulkan → cpu 回退。
- 验证：8 个 mock 测试通过，真机未验证。
- 状态：FIXED_UNVERIFIED

### ISS-005：SQLite 迁移
- 修复：sqlite3 backup API + 临时文件 + 原子替换。
- 验证：5 个 mock 测试通过（含 integrity_check），真机未验证。
- 状态：FIXED_UNVERIFIED

### ISS-006：Qwen Key 迁移
- 修复：migrate_legacy_user_data() 加 .qwen_key 迁移。
- 验证：3 个 mock 测试通过，真机未验证。
- 状态：FIXED_UNVERIFIED

### ISS-007：lol_data 路径统一
- 修复：lol_data_dir() 统一入口，所有读取方改用。
- 验证：3 个 mock 测试通过，真机未验证。
- 状态：FIXED_UNVERIFIED

### ISS-008：SSL certifi context
- 修复：overlay_assets.py 显式 ssl.create_default_context(cafile=certifi.where())。
- 验证：代码审查通过，真机下载未验证。
- 状态：FIXED_UNVERIFIED

### ISS-009：mpv 路径回退
- 修复：config.py mpv_path 失效自动回退到 bundled mpv。
- 验证：代码审查通过，真机未验证。
- 状态：FIXED_UNVERIFIED

### ISS-010：current_game_panel c 变量初始化
- 修复：c = None 提前初始化。
- 验证：静态检查测试通过。
- 状态：TESTED（静态）

## 真实使用问题台账（2026-10-30）

状态统一使用：待调查、已定位、待实施、实施中、自动化测试通过、待实机验收、已验收、暂缓。

| 编号 | 类型 | 当前状态 | 当前结论 / 下一步 |
|---|---|---|---|
| P1 | Bug：召唤师技能记录缺少语音反馈 | 自动化测试通过 | 根因是 `cooldown.start` 执行结果被语音层丢弃；现在成功/失败分别反馈，保留原计时规则。待 LCU/游戏内语音实机验收。 |
| P2 | 用户体验：电影搜索结果混杂 | 自动化测试通过 | 新增保守分类：来源显式类型优先，标题/备注仅识别明显的解说、短剧、翻拍短剧，其余为未知；UI 可筛选并持久化偏好，筛选不删除原始结果。待多源真实数据和 Windows UI 验收。 |
| P3 | 新功能：系统声音/麦克风识别音乐 | 待实机验收 | 已完成本地识曲闭环，并在识曲弹窗增加用户明确点击的“建立/更新指纹库”后台操作；当前配置目录可扫描 191 个受支持音频文件，`data/music/fingerprints.db` 初始为 0 首。建库完成显示新增/跳过/失败数量，完成后不自动录音；仍待 Windows 实机先建库、再分别验证两种来源命中、无匹配、取消清理和播放确认；仍无在线任意歌曲识别能力。 |
| P4 | Bug：播放模式记忆不准确 | 待调查 | 核对唯一状态、持久化、启动恢复和切换重置路径。 |
| P5 | Bug：托盘退出后 mpv 残留 | 自动化测试通过 | 托盘退出调用 `Application.stop()`；SYSTEM_SHUTDOWN 收口电影 mpv，音乐模块 stop 收口自身 mpv。相关退出/启动失败测试 48 passed；待 Windows 真实 PID 验收。 |
| P6 | 新功能：音乐/电影/游戏选择性加载 | 待调查 | 评估启动依赖和配置兼容性，分阶段改造。 |
| P7 | Bug：电影暂停时无法语音切下一集 | 自动化测试通过 | 根因是 VoiceRuntime 在暂停时直接拒绝命令；现已允许切集，并由 MovieController 保留切换前暂停状态。待播放中/暂停中 Windows+mpv 验收。 |

上述条目在完成真实环境验收前不得标记为“已验收”。

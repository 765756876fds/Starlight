# Starlight 2.0 — 已知问题清单

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
| P2 | 用户体验：电影搜索结果混杂 | 待调查 | 先盘点多源元数据和 UI 展示，再设计最小分类偏好。 |
| P3 | 新功能：系统声音/麦克风识别音乐 | 待调查 | 评估 Windows 回环采集、指纹服务、隐私和网络成本。 |
| P4 | Bug：播放模式记忆不准确 | 待调查 | 核对唯一状态、持久化、启动恢复和切换重置路径。 |
| P5 | Bug：托盘退出后 mpv 残留 | 自动化测试通过 | 托盘退出调用 `Application.stop()`；SYSTEM_SHUTDOWN 收口电影 mpv，音乐模块 stop 收口自身 mpv。相关退出/启动失败测试 48 passed；待 Windows 真实 PID 验收。 |
| P6 | 新功能：音乐/电影/游戏选择性加载 | 待调查 | 评估启动依赖和配置兼容性，分阶段改造。 |
| P7 | Bug：电影暂停时无法语音切下一集 | 自动化测试通过 | 根因是 VoiceRuntime 在暂停时直接拒绝命令；现已允许切集，并由 MovieController 保留切换前暂停状态。待播放中/暂停中 Windows+mpv 验收。 |

上述条目在完成真实环境验收前不得标记为“已验收”。
